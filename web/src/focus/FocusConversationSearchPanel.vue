<script lang="ts">
import type { FocusConversationSearchOccurrence } from './types';

export interface ConversationSearchSnippetParts {
  before: string;
  match: string;
  after: string;
}

/** Slice an admitted upstream UTF-16 range without reparsing or HTML injection. */
export function splitConversationSearchSnippet(
  occurrence: FocusConversationSearchOccurrence,
): ConversationSearchSnippetParts {
  const { snippet, snippet_match_range: range } = occurrence;
  return {
    before: snippet.slice(0, range.start),
    match: snippet.slice(range.start, range.end),
    after: snippet.slice(range.end),
  };
}
</script>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import Button from '../components/ui/Button.vue';
import Input from '../components/ui/Input.vue';
import PanelHeader from '../components/ui/PanelHeader.vue';
import type {
  FocusThreadConversationSearchPage,
  FocusThreadInspectionUnavailableReason,
} from './types';

const props = defineProps<{
  unavailableReason: FocusThreadInspectionUnavailableReason | null;
  loading: boolean;
  error: boolean;
  page: FocusThreadConversationSearchPage | null;
}>();

const emit = defineEmits<{
  close: [];
  search: [query: string];
  next: [];
  select: [occurrence: FocusConversationSearchOccurrence];
}>();

const { t } = useI18n();
const query = ref('');
const available = computed(() => props.unavailableReason === null);
const unavailableMessageKey = computed(() => {
  const reason = props.unavailableReason;
  if (reason === null) return '';
  const keys: Record<FocusThreadInspectionUnavailableReason, string> = {
    build_unsupported: 'focus.conversationSearchUnavailableBuild',
    document_unavailable: 'focus.conversationSearchUnavailableDocument',
    legacy_history: 'focus.conversationSearchUnavailableLegacy',
    no_active_thread: 'focus.conversationSearchUnavailableNoThread',
    runtime_unsupported: 'focus.conversationSearchUnavailableRuntime',
    thread_not_materialized: 'focus.conversationSearchUnavailableMaterializing',
    unknown_history: 'focus.conversationSearchUnavailableUnknown',
  };
  return keys[reason];
});
const normalizedQuery = computed(() => query.value.trim());
const queryLength = computed(() => Array.from(normalizedQuery.value).length);
const queryInvalid = computed(() => queryLength.value > 256);
const results = computed(() => (
  props.page?.occurrences.map((occurrence, index) => ({
    occurrence,
    parts: splitConversationSearchSnippet(occurrence),
    key: [
      index,
      occurrence.turn_cursor,
      occurrence.snippet_match_range.start,
      occurrence.snippet_match_range.end,
    ].join(':'),
  })) ?? []
));
const canSubmit = computed(() => (
  available.value
  && !props.loading
  && queryLength.value > 0
  && !queryInvalid.value
));

function submit(): void {
  if (canSubmit.value) emit('search', normalizedQuery.value);
}
</script>

<template>
  <div class="conversation-search-panel">
    <PanelHeader
      :title="t('focus.conversationSearchTitle')"
      :close-label="t('thinking.close')"
      @close="emit('close')"
    />
    <form class="conversation-search-form" @submit.prevent="submit">
      <Input
        v-model="query"
        :placeholder="t('focus.conversationSearchPlaceholder')"
        :disabled="!available"
        :error="queryInvalid"
        :aria-label="t('focus.conversationSearchTitle')"
      />
      <Button type="submit" :disabled="!canSubmit" :loading="loading">
        {{ t('focus.conversationSearchSubmit') }}
      </Button>
    </form>
    <p class="conversation-search-scope">
      {{ t('focus.conversationSearchScope') }}
    </p>
    <p v-if="unavailableReason" class="conversation-search-state" role="note">
      {{ t(unavailableMessageKey) }}
    </p>
    <p v-else-if="queryInvalid" class="conversation-search-error" role="alert">
      {{ t('focus.conversationSearchQueryTooLong') }}
    </p>
    <p v-else-if="error" class="conversation-search-error" role="alert">
      {{ t('focus.conversationSearchFailed') }}
    </p>

    <div class="conversation-search-results" :aria-busy="loading">
      <button
        v-for="result in results"
        :key="result.key"
        type="button"
        class="conversation-search-result"
        @click="emit('select', result.occurrence)"
      >
        <span>{{ result.parts.before }}</span><mark>{{ result.parts.match }}</mark><span>{{ result.parts.after }}</span>
      </button>
      <p
        v-if="page && page.occurrences.length === 0 && !loading"
        class="conversation-search-state"
      >
        {{ t('focus.conversationSearchEmpty') }}
      </p>
    </div>

    <div v-if="page?.next_cursor" class="conversation-search-footer">
      <Button
        variant="secondary"
        size="sm"
        :loading="loading"
        @click="emit('next')"
      >
        {{ t('focus.conversationSearchNext') }}
      </Button>
    </div>
  </div>
</template>

<style scoped>
.conversation-search-panel {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
}
.conversation-search-form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-3) 0;
}
.conversation-search-scope,
.conversation-search-state,
.conversation-search-error {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  line-height: var(--leading-relaxed);
}
.conversation-search-error { color: var(--color-danger); }
.conversation-search-results {
  flex: 1;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3) var(--space-3);
}
.conversation-search-result {
  width: 100%;
  padding: var(--space-3);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-md);
  background: var(--color-surface-raised);
  color: var(--color-text);
  font: var(--text-sm) / var(--leading-relaxed) var(--font-ui);
  text-align: left;
  overflow-wrap: anywhere;
  cursor: pointer;
}
.conversation-search-result:hover { border-color: var(--color-line-strong); }
.conversation-search-result:focus-visible {
  outline: none;
  box-shadow: var(--p-focus-ring);
}
.conversation-search-result mark {
  border-radius: var(--radius-xs);
  background: var(--color-accent-soft);
  color: var(--color-accent-hover);
}
.conversation-search-footer {
  flex: none;
  display: flex;
  justify-content: flex-end;
  padding: var(--space-2) var(--space-3) var(--space-3);
  border-top: 1px solid var(--color-line);
}
@media (max-width: 640px) {
  .conversation-search-form { grid-template-columns: 1fr; }
}
</style>
