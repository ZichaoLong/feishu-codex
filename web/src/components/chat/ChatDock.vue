<!-- ChatDock.vue -->
<!-- Bottom dock that belongs to the chat tab: goal strip, running-task chips, -->
<!-- pending question/approval cards, and the composer. Only rendered inside a -->
<!-- chat-pane group so it never leaks into files/tasks/preview/btw panes. -->
<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { ActivationBadges, AppGoal, AppModel, AppSkill, ApprovalBlock, ComposerCapabilities, ComposerSurfaceMode, ConversationStatus, PermissionMode, QuestionResponse, TaskItem, ThinkingLevel, TodoView, UIQuestion } from '../../types';
import type { FileItem } from './MentionMenu.vue';
import type { AttachmentUploadController } from '../../composables/useAttachmentUpload';
import Composer from './Composer.vue';
import type { ComposerSubmission } from './composerSubmission';
import GoalStrip from './GoalStrip.vue';
import QuestionCard from './QuestionCard.vue';
import ApprovalCard from './ApprovalCard.vue';
import TasksPane from './TasksPane.vue';
import TodoCard from './TodoCard.vue';
import Icon from '../ui/Icon.vue';
import Pill from '../ui/Pill.vue';

const props = withDefaults(defineProps<{
  sessionId?: string;
  /** Confirmed writer-scope readiness; independent from running/starting. */
  composerReady?: boolean;
  running?: boolean;
  interruptEnabled?: boolean;
  /** True while the empty-composer first prompt is being created + submitted.
   *  Covers the gap where draft-session creation already selected the new
   *  session (empty state → dock) before the first prompt is submitted. */
  starting?: boolean;
  searchFiles?: (q: string) => Promise<FileItem[]>;
  uploadImage?: (file: Blob, name?: string) => Promise<{ fileId: string; name: string; mediaType: string } | null>;
  downloadFile?: (fileId: string) => Promise<Blob>;
  attachmentUpload?: AttachmentUploadController;
  status: ConversationStatus;
  thinking?: ThinkingLevel;
  planMode?: boolean;
  swarmMode?: boolean;
  goalMode?: boolean;
  activationBadges?: ActivationBadges;
  models?: AppModel[];
  composerModelSettingsHint?: string;
  starredIds?: string[];
  skills?: AppSkill[];
  goal?: AppGoal | null;
  /** False keeps the goal readable but withholds all goal mutations. */
  canControlGoal?: boolean;
  goalExpandSignal?: number;
  dockPanel: 'bash' | 'subagent' | 'todos' | null;
  bashTasks: TaskItem[];
  subagentTasks: TaskItem[];
  bashRunning: number;
  subagentRunning: number;
  todoDoneCount: number;
  hasDockWork: boolean;
  todos?: TodoView[];
  pendingQuestion?: UIQuestion;
  /** Action kind in flight for the visible question (drives loading state). */
  questionBusyKind?: 'answer' | 'dismiss';
  pendingApproval?: {
    approvalId: string;
    block: ApprovalBlock;
    agentName?: string;
    actions?: { id: string; label: string; style: 'primary' | 'secondary' | 'danger' }[];
  };
  /** True while the visible approval has a respond in flight. */
  approvalBusy?: boolean;
  /** Whether this frontend may answer the currently visible interaction. */
  interactionEnabled?: boolean;
  mobile?: boolean;
  surfaceMode?: ComposerSurfaceMode;
  allowHide?: boolean;
  composerCapabilities?: Partial<ComposerCapabilities>;
  deferSubmitClear?: boolean;
}>(), {
  composerReady: true,
  surfaceMode: 'compact',
  allowHide: false,
});

const emit = defineEmits<{
  submit: [payload: ComposerSubmission];
  command: [cmd: string];
  interrupt: [];
  setPermission: [mode: PermissionMode];
  setThinking: [level: ThinkingLevel];
  togglePlan: [];
  toggleSwarm: [];
  toggleGoal: [];
  openBtw: [];
  createGoal: [objective: string];
  controlGoal: [action: 'pause' | 'resume' | 'cancel'];
  focusGoal: [];
  focusSwarm: [];
  compact: [];
  pickModel: [];
  selectModel: [modelId: string];
  answer: [questionId: string, response: QuestionResponse];
  dismiss: [questionId: string];
  approval: [approvalId: string, response: {
    decision: 'approved' | 'rejected' | 'cancelled';
    scope?: 'session';
    feedback?: string;
    selectedLabel?: string;
    actionId?: string;
  }];
  cancelTask: [taskId: string];
  'toggle-dock-panel': [panel: 'bash' | 'subagent' | 'todos'];
  'close-dock-panel': [];
  /** A background subagent chip was clicked — open its live detail panel. */
  openAgent: [taskId: string];
  surfaceModeChange: [mode: ComposerSurfaceMode];
  draftState: [hasDraft: boolean];
  requestInput: [];
}>();

const { t } = useI18n();
const composerRef = ref<{
  loadForEdit: (value: string) => void;
  loadRecovery: (value: string) => boolean;
  focus: () => void;
} | null>(null);
const workPanelRef = ref<HTMLElement | null>(null);
const workbarRef = ref<HTMLElement | null>(null);

function loadForEdit(value: string): boolean {
  // A pending interaction visually replaces the Composer but keeps the exact
  // submission owner mounted until an in-flight mutation settles. Keep edit
  // handoff unavailable so callers never dequeue into that hidden surface.
  const composer = availableComposer();
  if (!composer) return false;
  composer.loadForEdit(value);
  return true;
}

function loadRecovery(value: string): boolean {
  return availableComposer()?.loadRecovery(value) === true;
}

function focus(): void {
  availableComposer()?.focus();
}

function availableComposer(): typeof composerRef.value {
  if (props.pendingQuestion || props.pendingApproval) return null;
  return composerRef.value;
}

function onDocumentMouseDown(event: MouseEvent): void {
  if (!props.dockPanel) return;
  const target = event.target as Node | null;
  if (!target) return;
  if (workPanelRef.value?.contains(target)) return;
  if (workbarRef.value?.contains(target)) return;
  emit('close-dock-panel');
}

watch(
  () => props.dockPanel,
  (panel) => {
    if (typeof document === 'undefined') return;
    document.removeEventListener('mousedown', onDocumentMouseDown, true);
    if (panel) document.addEventListener('mousedown', onDocumentMouseDown, true);
  },
  { immediate: true },
);

onUnmounted(() => {
  if (typeof document !== 'undefined') {
    document.removeEventListener('mousedown', onDocumentMouseDown, true);
  }
});

defineExpose({
  loadForEdit,
  loadRecovery,
  focus,
});
</script>

<template>
  <div
    class="chat-dock"
    :class="[
      mobile ? 'align-mobile' : 'align-center',
      { 'composer-hidden': surfaceMode === 'hidden' && !pendingQuestion && !pendingApproval },
    ]"
    @click.stop
  >
    <Transition name="dock-panel">
      <div
        ref="workPanelRef"
        v-if="dockPanel"
        class="dock-work-panel"
        @click.stop
      >
        <div class="dock-work-head">
          <span
            v-if="dockPanel === 'bash'"
            class="dock-work-tab static"
          >
            {{ t('tasks.dockBash') }} · {{ bashRunning }} {{ t('tasks.running') }}
          </span>
          <span
            v-else-if="dockPanel === 'subagent'"
            class="dock-work-tab static"
          >
            {{ t('tasks.dockSubagent') }} · {{ subagentRunning }} {{ t('tasks.running') }}
          </span>
          <span
            v-else-if="dockPanel === 'todos'"
            class="dock-work-tab static"
          >
            {{ t('tasks.dockTodos') }} · {{ todoDoneCount }}/{{ todos?.length ?? 0 }}
          </span>
        </div>
        <div class="dock-work-body">
          <TasksPane
            v-if="dockPanel === 'bash'"
            :tasks="bashTasks"
          />
          <TasksPane
            v-else-if="dockPanel === 'subagent'"
            :tasks="subagentTasks"
            @open="emit('openAgent', $event)"
          />
          <TodoCard
            v-else-if="dockPanel === 'todos'"
            :todos="todos ?? []"
          />
        </div>
      </div>
    </Transition>

    <GoalStrip
      v-if="goal"
      :goal="goal"
      :can-control="canControlGoal ?? true"
      :force-expanded="goalExpandSignal"
      @control-goal="emit('controlGoal', $event)"
    />
    <div v-if="hasDockWork" ref="workbarRef" class="dock-workbar">
      <Pill
        v-if="bashTasks.length > 0"
        :active="dockPanel === 'bash'"
        :aria-pressed="dockPanel === 'bash'"
        @click="emit('toggle-dock-panel', 'bash')"
      >
        <Icon name="clock" size="md" />
        <span>{{ t('tasks.dockBash') }}</span>
        <span class="dw-count">(<b>{{ bashTasks.length }}</b>)</span>
      </Pill>
      <Pill
        v-if="subagentTasks.length > 0"
        :active="dockPanel === 'subagent'"
        :aria-pressed="dockPanel === 'subagent'"
        @click="emit('toggle-dock-panel', 'subagent')"
      >
        <Icon name="sparkles" size="md" />
        <span>{{ t('tasks.dockSubagent') }}</span>
        <span class="dw-count">(<b>{{ subagentTasks.length }}</b>)</span>
      </Pill>
      <Pill
        v-if="(todos?.length ?? 0) > 0"
        :active="dockPanel === 'todos'"
        :aria-pressed="dockPanel === 'todos'"
        @click="emit('toggle-dock-panel', 'todos')"
      >
        <Icon name="check-list" size="md" />
        <span>{{ t('tasks.dockTodos') }}</span>
        <span class="dw-count">(<b>{{ todoDoneCount }}/{{ todos?.length ?? 0 }}</b>)</span>
      </Pill>
    </div>

    <QuestionCard
      v-if="pendingQuestion"
      :key="pendingQuestion.questionId"
      :question="pendingQuestion"
      :busy-kind="questionBusyKind"
      :interaction-enabled="interactionEnabled"
      @answer="(qid, resp) => emit('answer', qid, resp)"
      @dismiss="emit('dismiss', $event)"
    />
    <ApprovalCard
      v-else-if="pendingApproval"
      :key="pendingApproval.approvalId"
      class="dock-approval"
      :block="pendingApproval.block"
      :agent-name="pendingApproval.agentName"
      :actions="pendingApproval.actions"
      :busy="approvalBusy"
      :interaction-enabled="interactionEnabled"
      @decide="emit('approval', pendingApproval!.approvalId, $event)"
    />
    <Composer
      v-show="!pendingQuestion && !pendingApproval"
      ref="composerRef"
      :session-id="sessionId"
      :composer-ready="composerReady"
      :running="running"
      :interrupt-enabled="interruptEnabled"
      :search-files="searchFiles"
      :upload-image="uploadImage"
      :download-file="downloadFile"
      :attachment-upload="attachmentUpload"
      :status="status"
      :thinking="thinking"
      :plan-mode="planMode"
      :swarm-mode="swarmMode"
      :goal-mode="goalMode"
      :goal="goal"
      :activation-badges="activationBadges"
      :models="models"
      :model-settings-hint="composerModelSettingsHint"
      :starred-ids="starredIds"
      :skills="skills"
      :starting="starting"
      :capabilities="composerCapabilities"
      :defer-submit-clear="deferSubmitClear"
      :mobile="mobile"
      :surface-mode="surfaceMode"
      :allow-hide="allowHide"
      :interaction-pending="!!pendingQuestion || !!pendingApproval"
      @submit="emit('submit', $event)"
      @command="emit('command', $event)"
      @interrupt="emit('interrupt')"
      @set-permission="emit('setPermission', $event)"
      @set-thinking="emit('setThinking', $event)"
      @toggle-plan="emit('togglePlan')"
      @toggle-swarm="emit('toggleSwarm')"
      @toggle-goal="emit('toggleGoal')"
      @open-btw="emit('openBtw')"
      @create-goal="emit('createGoal', $event)"
      @control-goal="emit('controlGoal', $event)"
      @focus-goal="emit('focusGoal')"
      @focus-swarm="emit('focusSwarm')"
      @compact="emit('compact')"
      @pick-model="emit('pickModel')"
      @select-model="emit('selectModel', $event)"
      @surface-mode-change="emit('surfaceModeChange', $event)"
      @draft-state="emit('draftState', $event)"
      @request-input="emit('requestInput')"
    />
  </div>
</template>

<style scoped>
.chat-dock {
  --dock-inline-left: 16px;
  --dock-inline-right: 16px;
  box-sizing: border-box;
  width: 100%;
  max-width: calc(var(--read-max) + var(--panes-scrollbar-width, 0px));
  padding-right: var(--panes-scrollbar-width, 0px);
  flex: none;
  position: relative;
  background: var(--color-bg);
  z-index: var(--z-sticky);
}
.chat-dock.align-center { margin-left: auto; margin-right: auto; }
.chat-dock.align-left { margin-left: 0; margin-right: auto; }
.chat-dock.align-mobile { max-width: none; }

.dock-work-panel {
  position: absolute;
  left: 16px;
  right: calc(16px + var(--panes-scrollbar-width, 0px));
  bottom: 100%;
  background: var(--color-surface);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-md);
  margin-bottom: 7px;
  max-height: min(360px, 50vh);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.dock-work-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-line);
}
.dock-work-tab {
  font-size: var(--text-base);
  font-weight: 500;
  color: var(--color-text);
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  background: var(--color-surface-sunken);
  border: 1px solid var(--color-line);
}
.dock-work-tab.static {
  background: transparent;
  border-color: transparent;
  padding-left: 2px;
}
.dock-work-body {
  padding: 8px 10px;
  overflow-y: auto;
  min-height: 0;
}
.dock-work-body :deep(.taskspane) {
  border: none;
  background: transparent;
  padding: 0;
}
.dock-work-body :deep(.taskspane .tp-head) {
  display: none;
}

.dock-workbar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px var(--dock-inline-right) 2px var(--dock-inline-left);
}
.dock-workbar .dw-count { margin-left: 1px; }
.dock-workbar .dw-count b { font-weight: 500; }

.dock-approval {
  margin-top: 8px;
}

@media (max-width: 640px) {
  .chat-dock {
    /* Inline (landscape) safe-area lives here only; the inner composer /
       workbar read --dock-inline-* so the inset is applied exactly once. */
    --dock-inline-left: max(12px, var(--safe-left));
    --dock-inline-right: max(12px, var(--safe-right));
  }
  .chat-dock.align-mobile.composer-hidden {
    padding-bottom: max(var(--space-3), var(--safe-bottom));
  }
  .dock-work-panel {
    left: 10px;
    right: calc(10px + var(--panes-scrollbar-width, 0px));
  }
}

.chat-dock:not(.align-mobile) :deep(.composer) {
  padding-bottom: 14px;
}

.dock-panel-enter-active,
.dock-panel-leave-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
}
.dock-panel-enter-from,
.dock-panel-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>
