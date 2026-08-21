<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import Button from '../components/ui/Button.vue';
import Dialog from '../components/ui/Dialog.vue';
import Select from '../components/ui/Select.vue';

type ReviewKind = 'uncommittedChanges' | 'baseBranch' | 'commit' | 'custom';

const props = defineProps<{
  open: boolean;
  loading?: boolean;
  enabled?: boolean;
}>();

const emit = defineEmits<{
  'update:open': [value: boolean];
  submit: [target: Record<string, unknown>];
}>();

const { t } = useI18n();
const kind = ref<ReviewKind>('uncommittedChanges');
const branch = ref('main');
const sha = ref('');
const title = ref('');
const instructions = ref('');

watch(
  () => props.open,
  (open) => {
    if (!open) return;
    kind.value = 'uncommittedChanges';
    branch.value = 'main';
    sha.value = '';
    title.value = '';
    instructions.value = '';
  },
);

const valid = computed(() => {
  if (kind.value === 'baseBranch') return !!branch.value.trim();
  if (kind.value === 'commit') return !!sha.value.trim();
  if (kind.value === 'custom') return !!instructions.value.trim();
  return true;
});

function submit(): void {
  if (!valid.value || props.loading || props.enabled === false) return;
  if (kind.value === 'uncommittedChanges') {
    emit('submit', { type: kind.value });
  } else if (kind.value === 'baseBranch') {
    emit('submit', { type: kind.value, branch: branch.value.trim() });
  } else if (kind.value === 'commit') {
    emit('submit', {
      type: kind.value,
      sha: sha.value.trim(),
      title: title.value.trim() || undefined,
    });
  } else {
    emit('submit', { type: kind.value, instructions: instructions.value.trim() });
  }
}
</script>

<template>
  <Dialog
    :open="open"
    :title="t('focus.reviewTitle')"
    :description="t('focus.reviewDescription')"
    @update:open="emit('update:open', $event)"
  >
    <form class="review-form" @submit.prevent="submit">
      <label>
        <span>{{ t('focus.reviewTarget') }}</span>
        <Select v-model="kind" :disabled="loading || enabled === false">
          <option value="uncommittedChanges">{{ t('focus.reviewUncommitted') }}</option>
          <option value="baseBranch">{{ t('focus.reviewBaseBranch') }}</option>
          <option value="commit">{{ t('focus.reviewCommit') }}</option>
          <option value="custom">{{ t('focus.reviewCustom') }}</option>
        </Select>
      </label>
      <label v-if="kind === 'baseBranch'">
        <span>{{ t('focus.reviewBranch') }}</span>
        <input v-model="branch" type="text" :disabled="loading || enabled === false" />
      </label>
      <template v-else-if="kind === 'commit'">
        <label>
          <span>{{ t('focus.reviewSha') }}</span>
          <input v-model="sha" type="text" :disabled="loading || enabled === false" />
        </label>
        <label>
          <span>{{ t('focus.reviewCommitTitle') }}</span>
          <input v-model="title" type="text" :disabled="loading || enabled === false" />
        </label>
      </template>
      <label v-else-if="kind === 'custom'">
        <span>{{ t('focus.reviewInstructions') }}</span>
        <textarea v-model="instructions" rows="5" :disabled="loading || enabled === false" />
      </label>
      <div class="actions">
        <Button type="button" variant="secondary" @click="emit('update:open', false)">
          {{ t('focus.cancel') }}
        </Button>
        <Button type="submit" variant="primary" :loading="loading" :disabled="enabled === false || !valid">
          {{ t('focus.reviewStart') }}
        </Button>
      </div>
    </form>
  </Dialog>
</template>

<style scoped>
.review-form,
.review-form label {
  display: flex;
  flex-direction: column;
}
.review-form { gap: var(--space-4); }
.review-form label {
  gap: var(--space-2);
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}
input,
textarea {
  width: 100%;
  padding: var(--space-3);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-sm);
  outline: none;
  background: var(--color-surface-raised);
  color: var(--color-text);
  font: inherit;
}
textarea { resize: vertical; line-height: var(--leading-normal); }
input:focus,
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
