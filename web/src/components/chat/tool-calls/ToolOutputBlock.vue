<!-- Shared line-oriented tool output block. Keeps the mounted DOM bounded while
     preserving the beginning, end, and any server omission disclosure. -->
<script lang="ts">
export const TOOL_OUTPUT_HEAD_LINE_COUNT = 25;
export const TOOL_OUTPUT_TAIL_LINE_COUNT = 25;

export function hasPresentedToolOutput(
  lines: readonly string[] | undefined,
  omittedChars: number | undefined,
): boolean {
  return (lines?.length ?? 0) > 0
    || (Number.isSafeInteger(omittedChars) && Number(omittedChars) > 0);
}

function serverOmissionLine(omittedChars: number): string | null {
  if (!Number.isSafeInteger(omittedChars) || omittedChars <= 0) return null;
  return `[Focus Web omitted ${omittedChars} characters of tool output; showing a bounded head and tail.]`;
}

export interface ToolOutputLineWindow {
  head: readonly string[];
  tail: readonly string[];
  omittedLineCount: number;
  boundedOmission: {
    section: 'head' | 'middle' | 'tail';
    lineIndex: number;
    omittedChars: number;
  } | null;
  aggregateOmittedChars: number;
}

export function buildToolOutputLineWindow(
  lines: readonly string[],
  serverOmittedChars = 0,
  serverHeadLineCount = 0,
): ToolOutputLineWindow {
  const trustedServerOmissionLine = serverOmissionLine(serverOmittedChars);
  if (
    lines.length === 0
    && trustedServerOmissionLine !== null
    && serverHeadLineCount === 0
  ) {
    return {
      head: [],
      tail: [],
      omittedLineCount: 0,
      boundedOmission: null,
      aggregateOmittedChars: serverOmittedChars,
    };
  }
  const markerIndex = (
    trustedServerOmissionLine !== null
    && Number.isSafeInteger(serverHeadLineCount)
    && serverHeadLineCount > 0
    && serverHeadLineCount < lines.length
    && lines[serverHeadLineCount] === trustedServerOmissionLine
  ) ? serverHeadLineCount : -1;
  const visibleLineCount = TOOL_OUTPUT_HEAD_LINE_COUNT + TOOL_OUTPUT_TAIL_LINE_COUNT;
  if (lines.length <= visibleLineCount) {
    return {
      head: lines,
      tail: [],
      omittedLineCount: 0,
      boundedOmission: markerIndex < 0 ? null : {
        section: 'head',
        lineIndex: markerIndex,
        omittedChars: serverOmittedChars,
      },
      aggregateOmittedChars: 0,
    };
  }

  const tailStart = lines.length - TOOL_OUTPUT_TAIL_LINE_COUNT;
  const boundedOmission = markerIndex < 0 ? null : markerIndex < TOOL_OUTPUT_HEAD_LINE_COUNT
    ? { section: 'head' as const, lineIndex: markerIndex, omittedChars: serverOmittedChars }
    : markerIndex >= tailStart
      ? {
          section: 'tail' as const,
          lineIndex: markerIndex - tailStart,
          omittedChars: serverOmittedChars,
        }
      : { section: 'middle' as const, lineIndex: 0, omittedChars: serverOmittedChars };

  return {
    head: lines.slice(0, TOOL_OUTPUT_HEAD_LINE_COUNT),
    tail: lines.slice(tailStart),
    omittedLineCount: lines.length - visibleLineCount
      - (boundedOmission?.section === 'middle' ? 1 : 0),
    boundedOmission,
    aggregateOmittedChars: 0,
  };
}
</script>

<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';

const props = defineProps<{
  lines?: string[];
  omittedChars?: number;
  headLineCount?: number;
  emptyText?: string;
}>();

const { t } = useI18n();
const outputWindow = computed(() => buildToolOutputLineWindow(
  props.lines ?? [],
  props.omittedChars ?? 0,
  props.headLineCount ?? 0,
));
</script>

<template>
  <div class="bb-code tool-output-block">
    <div
      v-if="outputWindow.head.length === 0
        && outputWindow.aggregateOmittedChars === 0
        && outputWindow.boundedOmission === null
        && emptyText"
      class="bb-empty"
    >
      {{ emptyText }}
    </div>
    <div
      v-for="(line, i) in outputWindow.head"
      :key="`head:${i}`"
      class="tool-output-line"
      :class="{ 'tool-output-server-omission': outputWindow.boundedOmission?.section === 'head' && outputWindow.boundedOmission.lineIndex === i }"
      :role="outputWindow.boundedOmission?.section === 'head' && outputWindow.boundedOmission.lineIndex === i ? 'note' : undefined"
    >
      {{ outputWindow.boundedOmission?.section === 'head' && outputWindow.boundedOmission.lineIndex === i
        ? t('tools.output.boundedOmitted', { count: outputWindow.boundedOmission.omittedChars })
        : line }}
    </div>
    <div v-if="outputWindow.omittedLineCount > 0" class="tool-output-omission" role="note">
      … {{ t('tools.output.linesOmitted', { count: outputWindow.omittedLineCount }) }} …
    </div>
    <div
      v-if="outputWindow.boundedOmission?.section === 'middle'"
      class="tool-output-line tool-output-server-omission"
      role="note"
    >
      {{ t('tools.output.boundedOmitted', { count: outputWindow.boundedOmission.omittedChars }) }}
    </div>
    <div
      v-if="outputWindow.aggregateOmittedChars > 0"
      class="tool-output-line tool-output-server-omission"
      role="note"
    >
      {{ t('tools.output.aggregateOmitted', { count: outputWindow.aggregateOmittedChars }) }}
    </div>
    <div
      v-for="(line, i) in outputWindow.tail"
      :key="`tail:${i}`"
      class="tool-output-line"
      :class="{ 'tool-output-server-omission': outputWindow.boundedOmission?.section === 'tail' && outputWindow.boundedOmission.lineIndex === i }"
      :role="outputWindow.boundedOmission?.section === 'tail' && outputWindow.boundedOmission.lineIndex === i ? 'note' : undefined"
    >
      {{ outputWindow.boundedOmission?.section === 'tail' && outputWindow.boundedOmission.lineIndex === i
        ? t('tools.output.boundedOmitted', { count: outputWindow.boundedOmission.omittedChars })
        : line }}
    </div>
  </div>
</template>

<style scoped>
.tool-output-block {
  margin-top: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-md);
  background: var(--color-surface-raised);
}
.bb-empty {
  color: var(--color-text-muted);
  font-style: italic;
}
.tool-output-omission {
  color: var(--color-text-muted);
  font-style: italic;
}
</style>
