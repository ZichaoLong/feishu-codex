<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import Banner from '../components/ui/Banner.vue';
import Button from '../components/ui/Button.vue';
import type { RuntimeNoticeItem } from './client-state/runtime-notices';
import type {
  UnknownLifecycleMutation,
  UnknownProcessLocalMutation,
} from './client-state/thread-mutations';
import type { UnknownSubmissionDraft } from './mutations/actions';
import type { FocusLifecycleOperation, FocusLifecycleTargetState } from './types';

const props = defineProps<{
  documentReloadRequired: boolean;
  backendResetOutcomeUnknown: boolean;
  errorMessage: string;
  primaryRuntimeErrors: readonly RuntimeNoticeItem[];
  primaryOperatorWarningCount: number;
  operatorErrorCount: number;
  operatorDegradedWithoutDetails: boolean;
  unknownSubmissionDrafts: readonly UnknownSubmissionDraft[];
  unknownProcessLocalMutations: readonly UnknownProcessLocalMutation[];
  unknownLifecycleMutations: readonly UnknownLifecycleMutation[];
  mutationBusyByThread: Record<string, true>;
  connection: string;
  canRecoverUnknownSubmission: (draft: UnknownSubmissionDraft) => boolean;
}>();

const emit = defineEmits<{
  reload: [];
  openRuntimeDetails: [];
  retryUnknownSubmission: [draft: UnknownSubmissionDraft];
  discardUnknownSubmission: [attemptKey: string];
  unlockProcessLocalMutation: [mutation: UnknownProcessLocalMutation];
  verifyLifecycleMutation: [threadId: string];
  unlockLifecycleMutation: [threadId: string];
}>();

const { t } = useI18n();

function lifecycleOperationLabel(operation: FocusLifecycleOperation): string {
  return t(`focus.lifecycleOperation${operation[0]!.toUpperCase()}${operation.slice(1)}`);
}

function lifecycleStateLabel(state: FocusLifecycleTargetState): string {
  return t(`focus.lifecycleState${state[0]!.toUpperCase()}${state.slice(1)}`);
}
</script>

<template>
  <div
    v-if="documentReloadRequired
      || backendResetOutcomeUnknown
      || errorMessage
      || primaryRuntimeErrors.length > 0
      || primaryOperatorWarningCount > 0
      || operatorDegradedWithoutDetails
      || unknownSubmissionDrafts.length > 0
      || unknownProcessLocalMutations.length > 0
      || unknownLifecycleMutations.length > 0"
    class="primary-notices"
  >
    <Banner v-if="documentReloadRequired" variant="danger">
      <span class="primary-notice-with-actions">
        <span>{{ t('focus.documentReplaced') }}</span>
        <Button size="sm" variant="secondary" @click="emit('reload')">
          {{ t('focus.reloadPage') }}
        </Button>
      </span>
    </Banner>

    <Banner v-if="backendResetOutcomeUnknown" variant="danger">
      {{ t('focus.backendResetOutcomeUnknown') }}
    </Banner>

    <Banner
      v-if="primaryOperatorWarningCount > 0 || operatorDegradedWithoutDetails"
      :variant="operatorErrorCount > 0 ? 'danger' : 'warning'"
    >
      <span class="primary-notice-with-actions">
        <span>
          {{ operatorDegradedWithoutDetails
            ? t('focus.operatorStatusDegraded')
            : t('focus.operatorCorrectnessWarnings', { count: primaryOperatorWarningCount }) }}
        </span>
        <Button size="sm" variant="ghost" @click="emit('openRuntimeDetails')">
          {{ t('focus.runtimeDetailsOpen') }}
        </Button>
      </span>
    </Banner>

    <Banner
      v-for="notice in primaryRuntimeErrors"
      :key="notice.id"
      variant="danger"
    >
      <span class="primary-notice-with-actions">
        <span class="primary-runtime-error">
          <strong>{{ t('focus.runtimeError') }}</strong>
          <span>{{ notice.message }}</span>
        </span>
        <Button size="sm" variant="ghost" @click="emit('openRuntimeDetails')">
          {{ t('focus.runtimeDetailsOpen') }}
        </Button>
      </span>
    </Banner>

    <Banner
      v-for="draft in unknownSubmissionDrafts"
      :key="draft.attemptKey"
      variant="warning"
    >
      <span class="primary-notice-with-actions">
        <span>
          {{ t(draft.recoveryBlocked
            ? 'focus.unknownDraftRecoveryBlocked'
            : 'focus.unknownFirstPromptDraftHeld', {
            threadId: draft.threadId || t('focus.unknown'),
          }) }}
        </span>
        <span class="primary-notice-actions">
          <Button
            v-if="canRecoverUnknownSubmission(draft)"
            size="sm"
            variant="secondary"
            @click="emit('retryUnknownSubmission', draft)"
          >
            {{ t('focus.restorePossiblySentDraft') }}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            @click="emit('discardUnknownSubmission', draft.attemptKey)"
          >
            {{ t('focus.discardUnknownDraft') }}
          </Button>
        </span>
      </span>
    </Banner>

    <Banner
      v-for="mutation in unknownProcessLocalMutations"
      :key="`unknown-control:${mutation.mutationId}`"
      variant="warning"
    >
      <span class="primary-notice-with-actions">
        <span>
          {{ t('focus.unknownControlHeld', {
            operation: mutation.operation,
            threadId: mutation.threadId,
          }) }}
        </span>
        <Button
          size="sm"
          variant="secondary"
          :disabled="connection !== 'connected'
            || mutationBusyByThread[mutation.threadId] === true"
          @click="emit('unlockProcessLocalMutation', mutation)"
        >
          {{ t('focus.unlockUnknownControl') }}
        </Button>
      </span>
    </Banner>

    <Banner
      v-for="mutation in unknownLifecycleMutations"
      :key="`unknown-lifecycle:${mutation.threadId}`"
      variant="warning"
    >
      <span class="primary-notice-with-actions">
        <span>
          {{ t('focus.unknownLifecycleHeld', {
            operation: lifecycleOperationLabel(mutation.operation),
            threadId: mutation.threadId,
          }) }}
          <template v-if="mutation.verification">
            {{ t('focus.unknownLifecycleVerified', {
              state: lifecycleStateLabel(mutation.verification.state),
            }) }}
          </template>
        </span>
        <Button
          v-if="!mutation.verification"
          size="sm"
          variant="secondary"
          :disabled="connection !== 'connected'
            || mutationBusyByThread[mutation.threadId] === true"
          @click="emit('verifyLifecycleMutation', mutation.threadId)"
        >
          {{ t('focus.verifyUnknownLifecycle') }}
        </Button>
        <Button
          v-else
          size="sm"
          variant="secondary"
          :disabled="connection !== 'connected'
            || mutationBusyByThread[mutation.threadId] === true"
          @click="emit('unlockLifecycleMutation', mutation.threadId)"
        >
          {{ t('focus.unlockUnknownLifecycle') }}
        </Button>
      </span>
    </Banner>

    <Banner v-if="errorMessage" variant="danger">{{ errorMessage }}</Banner>
  </div>
</template>

<style scoped>
.primary-notices {
  display: flex;
  flex: none;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3) 0;
}
.primary-notice-with-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  width: 100%;
}
.primary-notice-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: none;
}
.primary-runtime-error {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: var(--space-1);
  overflow-wrap: anywhere;
}
@media (max-width: 640px) {
  .primary-notices { padding-inline: max(var(--space-2), var(--safe-left)); }
  .primary-notice-with-actions { align-items: flex-start; flex-direction: column; }
}
</style>
