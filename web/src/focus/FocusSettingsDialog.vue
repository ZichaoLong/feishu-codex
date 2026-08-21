<script setup lang="ts">
import type { ColorScheme } from '../composables/client/useAppearance';
import LanguageSwitcher from '../components/settings/LanguageSwitcher.vue';
import Dialog from '../components/ui/Dialog.vue';
import SegmentedControl from '../components/ui/SegmentedControl.vue';
import Select from '../components/ui/Select.vue';
import Button from '../components/ui/Button.vue';
import Banner from '../components/ui/Banner.vue';
import Icon from '../components/ui/Icon.vue';
import { ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type {
  FocusBackendResetPreview,
  FocusBackendResetResult,
  FocusThreadSummary,
} from './types';

const props = defineProps<{
  open: boolean;
  colorScheme: ColorScheme;
  turnWindowLimit: number;
  connection: string;
  approvalPolicy: string;
  approvalPolicies: string[];
  reasoningEffort: string;
  reasoningEffortOptions: string[];
  permissionsProfileId: string;
  permissionsProfiles: { id: string; label: string }[];
  archivedThreads: FocusThreadSummary[];
  archivedLoading: boolean;
  archivedTruncated: boolean;
  archivedLimit: number;
  lifecycleBusyByThread: Record<string, true>;
  backendResetPreview: FocusBackendResetPreview | null;
  backendResetResult: FocusBackendResetResult | null;
  backendResetLoading: boolean;
  backendResetBusy: boolean;
  backendResetOutcomeUnknown: boolean;
}>();

const emit = defineEmits<{
  'update:open': [value: boolean];
  setColorScheme: [value: ColorScheme];
  setTurnWindowLimit: [value: number];
  setApprovalPolicy: [value: string];
  setReasoningEffort: [value: string];
  setPermissionsProfile: [value: string];
  refreshArchived: [];
  unarchive: [threadId: string];
  deleteThread: [threadId: string, confirmation: string];
  refreshBackendReset: [];
  confirmBackendReset: [preview: FocusBackendResetPreview];
}>();

const { t } = useI18n();

const themeOptions = [
  { value: 'system', label: 'System' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
];
const section = ref<'preferences' | 'archived' | 'danger'>('preferences');
const deleteTarget = ref('');
const deleteConfirmation = ref('');

watch(
  () => props.open,
  (open, wasOpen) => {
    if (!open) {
      deleteTarget.value = '';
      deleteConfirmation.value = '';
    } else if (!wasOpen && section.value === 'danger') {
      emit('refreshBackendReset');
    }
  },
);

function setSection(value: string): void {
  if (value !== 'preferences' && value !== 'archived' && value !== 'danger') return;
  const enteringDanger = value === 'danger' && section.value !== 'danger';
  section.value = value;
  if (value === 'archived') emit('refreshArchived');
  if (enteringDanger) emit('refreshBackendReset');
}

function backendResetStatusLabel(status: FocusBackendResetPreview['status']): string {
  if (status === 'available') return t('focus.backendResetStatusAvailable');
  if (status === 'force-only') return t('focus.backendResetStatusForceOnly');
  return t('focus.backendResetStatusUnavailable');
}

function requestBackendReset(): void {
  const preview = props.backendResetPreview;
  if (
    props.backendResetOutcomeUnknown
    || props.backendResetLoading
    || props.backendResetBusy
    || (preview?.status !== 'available' && preview?.status !== 'force-only')
  ) return;
  emit('confirmBackendReset', preview);
}

function canUnarchive(thread: FocusThreadSummary): boolean {
  return props.connection === 'connected' && thread.action_capabilities?.unarchive === true;
}

function canDelete(thread: FocusThreadSummary): boolean {
  return props.connection === 'connected' && thread.action_capabilities?.delete === true;
}

function requestDelete(thread: FocusThreadSummary): void {
  if (!canDelete(thread)) return;
  deleteTarget.value = thread.id;
  deleteConfirmation.value = '';
}

function cancelDelete(): void {
  deleteTarget.value = '';
  deleteConfirmation.value = '';
}

function confirmDelete(): void {
  if (!deleteTarget.value || deleteConfirmation.value.trim() !== deleteTarget.value) return;
  const target = props.archivedThreads.find((thread) => thread.id === deleteTarget.value);
  if (!target || !canDelete(target)) return;
  emit('deleteThread', deleteTarget.value, deleteConfirmation.value.trim());
}

function setTheme(value: string): void {
  if (value === 'system' || value === 'light' || value === 'dark') {
    emit('setColorScheme', value);
  }
}

function setTurnWindowLimit(value: string): void {
  const parsed = Number(value);
  if (parsed === 5 || parsed === 10 || parsed === 20) {
    emit('setTurnWindowLimit', parsed);
  }
}
</script>

<template>
  <Dialog
    :open="open"
    title="Focus Web"
    :description="t('focus.settingsDescription')"
    @update:open="emit('update:open', $event)"
  >
    <div class="settings-list">
      <SegmentedControl
        :model-value="section"
        :options="[
          { value: 'preferences', label: t('focus.runtime') },
          { value: 'archived', label: t('focus.archived') },
          { value: 'danger', label: t('focus.dangerZone') },
        ]"
        size="sm"
        @update:model-value="setSection"
      />

      <template v-if="section === 'preferences'">
        <section class="settings-row">
          <div>
            <div class="settings-label">{{ t('focus.theme') }}</div>
            <div class="settings-description">{{ t('focus.themeDescription') }}</div>
          </div>
          <SegmentedControl
            :model-value="colorScheme"
            :options="themeOptions"
            size="sm"
            @update:model-value="setTheme"
          />
        </section>
        <section class="settings-row">
          <div>
            <div class="settings-label">{{ t('focus.language') }}</div>
            <div class="settings-description">{{ t('focus.languageDescription') }}</div>
          </div>
          <LanguageSwitcher />
        </section>
        <section class="settings-row">
          <div>
            <div class="settings-label">{{ t('focus.turnWindow') }}</div>
            <div class="settings-description">{{ t('focus.turnWindowDescription') }}</div>
          </div>
          <Select
            class="settings-select"
            size="sm"
            :model-value="String(turnWindowLimit)"
            @update:model-value="setTurnWindowLimit"
          >
            <option value="5">5</option>
            <option value="10">10</option>
            <option value="20">20</option>
          </Select>
        </section>
        <section class="settings-row">
          <div>
            <div class="settings-label">{{ t('focus.approvalPolicy') }}</div>
            <div class="settings-description">{{ t('focus.approvalPolicyDescription') }}</div>
          </div>
          <Select
            class="settings-select"
            size="sm"
            :model-value="approvalPolicy"
            @update:model-value="emit('setApprovalPolicy', $event)"
          >
            <option v-for="policy in approvalPolicies" :key="policy" :value="policy">
              {{ policy }}
            </option>
          </Select>
        </section>
        <section class="settings-row">
          <div>
            <div class="settings-label">{{ t('focus.reasoningEffort') }}</div>
            <div class="settings-description">{{ t('focus.reasoningEffortDescription') }}</div>
          </div>
          <Select
            class="settings-select"
            size="sm"
            :model-value="reasoningEffort"
            @update:model-value="emit('setReasoningEffort', $event)"
          >
            <option value="">{{ t('focus.auto') }}</option>
            <option v-for="effort in reasoningEffortOptions" :key="effort" :value="effort">
              {{ effort }}
            </option>
          </Select>
        </section>
        <section class="settings-row">
          <div>
            <div class="settings-label">{{ t('focus.permissions') }}</div>
            <div class="settings-description">{{ t('focus.permissionsDescription') }}</div>
          </div>
          <Select
            class="settings-select"
            size="sm"
            :model-value="permissionsProfileId"
            @update:model-value="emit('setPermissionsProfile', $event)"
          >
            <option v-for="profile in permissionsProfiles" :key="profile.id" :value="profile.id">
              {{ profile.label }}
            </option>
          </Select>
        </section>
        <div class="next-turn-note">{{ t('focus.nextTurnSettings') }}</div>
      </template>

      <section v-else-if="section === 'archived'" class="archived-panel">
        <div class="archived-heading">
          <div>
            <div class="settings-label">{{ t('focus.archived') }}</div>
            <div class="settings-description">
              {{ archivedTruncated
                ? t('focus.archivedRecentDescription', { count: archivedLimit })
                : t('focus.archivedDescription') }}
            </div>
          </div>
          <Button size="sm" variant="secondary" :loading="archivedLoading" @click="emit('refreshArchived')">
            <Icon name="refresh" size="sm" />
            {{ t('focus.archivedRefresh') }}
          </Button>
        </div>
        <div v-if="!archivedLoading && archivedThreads.length === 0" class="archived-empty">
          {{ t('focus.archivedEmpty') }}
        </div>
        <div v-else class="archived-list">
          <article v-for="thread in archivedThreads" :key="thread.id" class="archived-row">
            <div class="archived-meta">
              <strong>{{ thread.title }}</strong>
              <span>{{ thread.cwd }}</span>
              <code>{{ thread.id }}</code>
            </div>
            <div
              v-if="deleteTarget !== thread.id && (canUnarchive(thread) || canDelete(thread))"
              class="archived-actions"
            >
              <Button
                size="sm"
                variant="secondary"
                :loading="!!lifecycleBusyByThread[thread.id]"
                v-if="canUnarchive(thread)"
                @click="emit('unarchive', thread.id)"
              >
                {{ t('focus.unarchive') }}
              </Button>
              <Button v-if="canDelete(thread)" size="sm" variant="danger-soft" @click="requestDelete(thread)">
                {{ t('focus.deletePermanently') }}
              </Button>
            </div>
            <form v-else-if="canDelete(thread)" class="delete-confirm" @submit.prevent="confirmDelete">
              <label>
                <span>{{ t('focus.deleteConfirmLabel') }}</span>
                <input v-model="deleteConfirmation" type="text" autocomplete="off" />
              </label>
              <div>
                <Button type="button" size="sm" variant="secondary" @click="cancelDelete">
                  {{ t('focus.cancel') }}
                </Button>
                <Button
                  type="submit"
                  size="sm"
                  variant="danger"
                  :loading="!!lifecycleBusyByThread[thread.id]"
                  :disabled="deleteConfirmation.trim() !== thread.id"
                >
                  {{ t('focus.deleteConfirm') }}
                </Button>
              </div>
            </form>
          </article>
        </div>
      </section>

      <section v-else class="danger-panel">
        <div>
          <div class="settings-label danger-label">{{ t('focus.dangerZone') }}</div>
          <div class="settings-description">{{ t('focus.dangerZoneDescription') }}</div>
        </div>

        <Banner v-if="backendResetOutcomeUnknown" variant="danger">
          {{ t('focus.backendResetOutcomeUnknown') }}
        </Banner>
        <Banner v-else-if="backendResetResult" variant="info">
          {{ t('focus.backendResetSucceeded') }}
        </Banner>

        <div
          v-if="backendResetResult && !backendResetOutcomeUnknown"
          class="backend-reset-result"
        >
          <dl>
            <div>
              <dt>{{ t('focus.backendResetDetachedBindings') }}</dt>
              <dd>{{ backendResetResult.detached_binding_count }}</dd>
            </div>
            <div>
              <dt>{{ t('focus.backendResetInterruptedBindings') }}</dt>
              <dd>{{ backendResetResult.interrupted_binding_count }}</dd>
            </div>
            <div>
              <dt>{{ t('focus.backendResetRetiredRequests') }}</dt>
              <dd>{{ backendResetResult.retired_request_count }}</dd>
            </div>
            <div>
              <dt>{{ t('focus.backendResetPurgedThreads') }}</dt>
              <dd>{{ backendResetResult.purged_thread_count }}</dd>
            </div>
          </dl>
          <Banner v-if="backendResetResult.projection_warnings.length > 0" variant="warning">
            <div>
              <div>{{ t('focus.backendResetProjectionWarnings') }}</div>
              <ul>
                <li
                  v-for="(warning, index) in backendResetResult.projection_warnings"
                  :key="`${index}:${warning}`"
                >
                  {{ warning }}
                </li>
              </ul>
            </div>
          </Banner>
        </div>

        <div class="backend-reset-card">
          <div class="backend-reset-heading">
            <div>
              <div class="settings-label">{{ t('focus.backendResetTitle') }}</div>
              <div class="settings-description">{{ t('focus.backendResetDescription') }}</div>
            </div>
            <Button
              size="sm"
              variant="secondary"
              :loading="backendResetLoading"
              :disabled="backendResetBusy"
              @click="emit('refreshBackendReset')"
            >
              <Icon name="refresh" size="sm" />
              {{ t('focus.backendResetRefresh') }}
            </Button>
          </div>

          <div v-if="backendResetPreview" class="backend-reset-preview">
            <dl>
              <div>
                <dt>{{ t('focus.backendResetStatus') }}</dt>
                <dd>{{ backendResetStatusLabel(backendResetPreview.status) }}</dd>
              </div>
              <div>
                <dt>{{ t('focus.backendResetInstance') }}</dt>
                <dd>{{ backendResetPreview.instance }}</dd>
              </div>
              <div>
                <dt>{{ t('focus.backendResetPendingRequests') }}</dt>
                <dd>{{ backendResetPreview.pending_request_count }}</dd>
              </div>
              <div>
                <dt>{{ t('focus.backendResetRunningBindings') }}</dt>
                <dd>{{ backendResetPreview.running_binding_count }}</dd>
              </div>
              <div>
                <dt>{{ t('focus.backendResetAttachedBindings') }}</dt>
                <dd>{{ backendResetPreview.attached_binding_count }}</dd>
              </div>
              <div>
                <dt>{{ t('focus.backendResetActiveLoadedThreads') }}</dt>
                <dd>{{ backendResetPreview.active_loaded_thread_count }}</dd>
              </div>
              <div>
                <dt>{{ t('focus.backendResetLoadedThreads') }}</dt>
                <dd>{{ backendResetPreview.loaded_thread_count }}</dd>
              </div>
            </dl>

            <Banner v-if="backendResetPreview.runtime_verification_failed" variant="warning">
              {{ t('focus.backendResetRuntimeVerificationFailed') }}
            </Banner>

            <div class="backend-reset-actions">
              <Button
                v-if="!backendResetOutcomeUnknown && backendResetPreview.status === 'available'"
                size="sm"
                variant="danger-soft"
                :loading="backendResetBusy"
                :disabled="backendResetLoading"
                @click="requestBackendReset"
              >
                {{ t('focus.backendResetConfirmSafe') }}
              </Button>
              <Button
                v-else-if="!backendResetOutcomeUnknown && backendResetPreview.status === 'force-only'"
                size="sm"
                variant="danger"
                :loading="backendResetBusy"
                :disabled="backendResetLoading"
                @click="requestBackendReset"
              >
                {{ t('focus.backendResetConfirmForce') }}
              </Button>
            </div>
          </div>
          <div v-else-if="!backendResetLoading" class="backend-reset-empty">
            {{ t('focus.backendResetNoPreview') }}
          </div>
        </div>
      </section>
    </div>
  </Dialog>
</template>

<style scoped>
.settings-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.settings-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding-bottom: var(--space-4);
  border-bottom: 1px solid var(--color-line);
}
.settings-label {
  color: var(--color-text);
  font-size: var(--text-base);
  font-weight: 500;
}
.settings-description {
  margin-top: var(--space-1);
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  line-height: var(--leading-normal);
}
.settings-select {
  width: min(210px, 100%);
  flex: none;
}
.next-turn-note {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-accent-bd);
  border-radius: var(--radius-md);
  background: var(--color-accent-soft);
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  line-height: var(--leading-normal);
}
.archived-panel,
.archived-list,
.archived-meta,
.delete-confirm,
.delete-confirm label {
  display: flex;
  flex-direction: column;
}
.archived-panel,
.archived-list { gap: var(--space-3); }
.archived-heading,
.archived-row,
.archived-actions,
.delete-confirm > div {
  display: flex;
  align-items: center;
}
.archived-heading,
.archived-row { justify-content: space-between; gap: var(--space-4); }
.archived-list {
  max-height: min(52vh, 520px);
  overflow-y: auto;
}
.archived-row {
  padding: var(--space-3) 0;
  border-top: 1px solid var(--color-line);
}
.archived-meta { min-width: 0; gap: 2px; }
.archived-meta strong,
.archived-meta span,
.archived-meta code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.archived-meta span,
.archived-meta code,
.delete-confirm label {
  color: var(--color-text-muted);
  font-size: var(--text-xs);
}
.archived-actions,
.delete-confirm > div { gap: var(--space-2); }
.delete-confirm { flex: 1; max-width: 420px; gap: var(--space-2); }
.delete-confirm label { gap: var(--space-1); }
.delete-confirm input {
  width: 100%;
  padding: var(--space-2);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-sm);
  outline: none;
  background: var(--color-surface-raised);
  color: var(--color-text);
  font-family: var(--font-mono);
}
.delete-confirm input:focus {
  border-color: var(--color-accent);
  box-shadow: var(--p-focus-ring);
}
.archived-empty {
  padding: var(--space-6) var(--space-3);
  border-top: 1px solid var(--color-line);
  color: var(--color-text-muted);
  text-align: center;
}
.danger-panel,
.backend-reset-card,
.backend-reset-preview,
.backend-reset-result {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.danger-label {
  color: var(--color-danger);
}
.backend-reset-card {
  padding: var(--space-4);
  border: 1px solid var(--color-danger-bd);
  border-radius: var(--radius-md);
}
.backend-reset-heading,
.backend-reset-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}
.backend-reset-preview dl,
.backend-reset-result dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-2) var(--space-4);
  margin: 0;
}
.backend-reset-preview dl div,
.backend-reset-result dl div {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  min-width: 0;
}
.backend-reset-preview dt,
.backend-reset-result dt,
.backend-reset-empty {
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}
.backend-reset-preview dd,
.backend-reset-result dd {
  margin: 0;
  color: var(--color-text);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}
.backend-reset-result ul {
  margin: var(--space-1) 0 0;
  padding-left: var(--space-4);
}
.backend-reset-actions {
  justify-content: flex-end;
}
.backend-reset-empty {
  padding: var(--space-4) 0;
  text-align: center;
}
@media (max-width: 640px) {
  .settings-row {
    align-items: flex-start;
    flex-direction: column;
  }
  .settings-select {
    width: 100%;
  }
  .archived-heading,
  .archived-row,
  .backend-reset-heading {
    align-items: stretch;
    flex-direction: column;
  }
  .backend-reset-preview dl,
  .backend-reset-result dl {
    grid-template-columns: minmax(0, 1fr);
  }
  .archived-actions,
  .delete-confirm > div {
    justify-content: flex-end;
  }
}
</style>
