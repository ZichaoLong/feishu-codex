<script setup lang="ts">
import { ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import Button from '../components/ui/Button.vue';
import Dialog from '../components/ui/Dialog.vue';

const props = defineProps<{
  open: boolean;
  objective?: string;
  loading?: boolean;
  enabled?: boolean;
}>();

const emit = defineEmits<{
  'update:open': [value: boolean];
  submit: [objective: string];
}>();

const { t } = useI18n();
const value = ref('');

watch(
  () => props.open,
  (open) => {
    if (open) value.value = props.objective?.trim() ?? '';
  },
);

function submit(): void {
  const objective = value.value.trim();
  if (!objective || props.loading || props.enabled === false) return;
  emit('submit', objective);
}
</script>

<template>
  <Dialog
    :open="open"
    :title="t('focus.goalTitle')"
    :description="t('focus.goalDescription')"
    @update:open="emit('update:open', $event)"
  >
    <form class="goal-form" @submit.prevent="submit">
      <label>
        <span>{{ t('focus.goalObjective') }}</span>
        <textarea
          v-model="value"
          rows="5"
          :placeholder="t('focus.goalPlaceholder')"
          :disabled="loading || enabled === false"
        />
      </label>
      <div class="actions">
        <Button type="button" variant="secondary" @click="emit('update:open', false)">
          {{ t('focus.cancel') }}
        </Button>
        <Button type="submit" variant="primary" :loading="loading" :disabled="enabled === false || !value.trim()">
          {{ t('focus.goalApply') }}
        </Button>
      </div>
    </form>
  </Dialog>
</template>

<style scoped>
.goal-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.goal-form label {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}
textarea {
  width: 100%;
  min-height: 132px;
  resize: vertical;
  padding: var(--space-3);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-sm);
  outline: none;
  background: var(--color-surface-raised);
  color: var(--color-text);
  font: inherit;
  line-height: var(--leading-normal);
}
textarea:focus {
  border-color: var(--color-accent);
  box-shadow: var(--p-focus-ring);
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
</style>
