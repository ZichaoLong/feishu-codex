<!-- apps/kimi-web/src/components/chat/DiffLines.vue -->
<!-- Pure line-by-line diff renderer used by bounded previews and explicit
     complete-source detail. Owns only rows + styling; the caller selects the
     presentation bound and controls the surrounding height / scroll. -->
<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import type { DiffViewLine } from '../../types';

const props = withDefaults(defineProps<{
  lines: DiffViewLine[];
  omittedChars?: number;
  omissionLineIndex?: number;
  mode?: 'bounded' | 'complete';
}>(), { mode: 'bounded' });

const { t } = useI18n();
const lineWindow = computed(() => (
  props.mode === 'complete'
    ? buildCompleteDiffLineWindow(props.lines)
    : buildDiffLineWindow(
        props.lines,
        props.omittedChars ?? 0,
        props.omissionLineIndex ?? 0,
      )
));

function oldGutter(line: DiffViewLine): string {
  return line.oldNo !== undefined ? String(line.oldNo) : '';
}
function newGutter(line: DiffViewLine): string {
  return line.newNo !== undefined ? String(line.newNo) : '';
}
function rowClass(line: DiffViewLine): string {
  return `dl-${line.type}`;
}
</script>

<script lang="ts">
export const DIFF_HEAD_LINE_COUNT = 25;
export const DIFF_TAIL_LINE_COUNT = 25;

export interface DiffLineWindow {
  head: readonly DiffViewLine[];
  tail: readonly DiffViewLine[];
  omittedLineCount: number;
  boundedOmission: {
    section: 'head' | 'middle' | 'tail';
    lineIndex: number;
    omittedChars: number;
  } | null;
  aggregateOmittedChars: number;
}

function buildCompleteDiffLineWindow(
  lines: readonly DiffViewLine[],
): DiffLineWindow {
  return {
    head: lines,
    tail: [],
    omittedLineCount: 0,
    boundedOmission: null,
    aggregateOmittedChars: 0,
  };
}

export function buildDiffLineWindow(
  lines: readonly DiffViewLine[],
  serverOmittedChars = 0,
  serverOmissionLineIndex = 0,
): DiffLineWindow {
  const expectedMarker = Number.isSafeInteger(serverOmittedChars) && serverOmittedChars > 0
    ? `[Focus Web omitted ${serverOmittedChars} characters of tool output; showing a bounded head and tail.]`
    : '';
  if (lines.length === 0 && expectedMarker && serverOmissionLineIndex === 0) {
    return {
      head: [],
      tail: [],
      omittedLineCount: 0,
      boundedOmission: null,
      aggregateOmittedChars: serverOmittedChars,
    };
  }
  const candidate = Number.isSafeInteger(serverOmissionLineIndex)
    && serverOmissionLineIndex > 0
    && serverOmissionLineIndex < lines.length
    ? lines[serverOmissionLineIndex]
    : undefined;
  const markerIndex = candidate?.text === expectedMarker ? serverOmissionLineIndex : -1;
  const visibleLineCount = DIFF_HEAD_LINE_COUNT + DIFF_TAIL_LINE_COUNT;
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
  const tailStart = lines.length - DIFF_TAIL_LINE_COUNT;
  const boundedOmission = markerIndex < 0 ? null : markerIndex < DIFF_HEAD_LINE_COUNT
    ? { section: 'head' as const, lineIndex: markerIndex, omittedChars: serverOmittedChars }
    : markerIndex >= tailStart
      ? {
          section: 'tail' as const,
          lineIndex: markerIndex - tailStart,
          omittedChars: serverOmittedChars,
        }
      : { section: 'middle' as const, lineIndex: 0, omittedChars: serverOmittedChars };
  return {
    head: lines.slice(0, DIFF_HEAD_LINE_COUNT),
    tail: lines.slice(tailStart),
    omittedLineCount: lines.length - visibleLineCount
      - (boundedOmission?.section === 'middle' ? 1 : 0),
    boundedOmission,
    aggregateOmittedChars: 0,
  };
}
</script>

<template>
  <div class="diff-lines">
    <div
      v-for="(line, i) in lineWindow.head"
      :key="`head:${i}`"
      class="dl"
      :class="lineWindow.boundedOmission?.section === 'head' && lineWindow.boundedOmission.lineIndex === i ? ['dl-hunk', 'dl-server-omission'] : rowClass(line)"
      :role="lineWindow.boundedOmission?.section === 'head' && lineWindow.boundedOmission.lineIndex === i ? 'note' : undefined"
    >
      <template v-if="lineWindow.boundedOmission?.section === 'head' && lineWindow.boundedOmission.lineIndex === i">
        <span class="hunk-text">{{ t('tools.output.boundedOmitted', { count: lineWindow.boundedOmission.omittedChars }) }}</span>
      </template>
      <template v-else-if="line.type === 'hunk'">
        <span class="hunk-text">{{ line.text }}</span>
      </template>
      <template v-else>
        <span class="dl-gutter old">{{ oldGutter(line) }}</span>
        <span class="dl-gutter new">{{ newGutter(line) }}</span>
        <span class="dl-sign">{{ line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' ' }}</span>
        <span class="dl-text">{{ line.text }}</span>
      </template>
    </div>
    <div v-if="lineWindow.omittedLineCount > 0" class="dl dl-omission" role="note">
      <span class="hunk-text">
        … {{ t('tools.output.linesOmitted', { count: lineWindow.omittedLineCount }) }} …
      </span>
    </div>
    <div
      v-if="lineWindow.boundedOmission?.section === 'middle'"
      class="dl dl-hunk dl-server-omission"
      role="note"
    >
      <span class="hunk-text">
        {{ t('tools.output.boundedOmitted', { count: lineWindow.boundedOmission.omittedChars }) }}
      </span>
    </div>
    <div
      v-if="lineWindow.aggregateOmittedChars > 0"
      class="dl dl-hunk dl-server-omission"
      role="note"
    >
      <span class="hunk-text">
        {{ t('tools.output.aggregateOmitted', { count: lineWindow.aggregateOmittedChars }) }}
      </span>
    </div>
    <div
      v-for="(line, i) in lineWindow.tail"
      :key="`tail:${i}`"
      class="dl"
      :class="lineWindow.boundedOmission?.section === 'tail' && lineWindow.boundedOmission.lineIndex === i ? ['dl-hunk', 'dl-server-omission'] : rowClass(line)"
      :role="lineWindow.boundedOmission?.section === 'tail' && lineWindow.boundedOmission.lineIndex === i ? 'note' : undefined"
    >
      <template v-if="lineWindow.boundedOmission?.section === 'tail' && lineWindow.boundedOmission.lineIndex === i">
        <span class="hunk-text">{{ t('tools.output.boundedOmitted', { count: lineWindow.boundedOmission.omittedChars }) }}</span>
      </template>
      <template v-else-if="line.type === 'hunk'">
        <span class="hunk-text">{{ line.text }}</span>
      </template>
      <template v-else>
        <span class="dl-gutter old">{{ oldGutter(line) }}</span>
        <span class="dl-gutter new">{{ newGutter(line) }}</span>
        <span class="dl-sign">{{ line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' ' }}</span>
        <span class="dl-text">{{ line.text }}</span>
      </template>
    </div>
  </div>
</template>

<style scoped>
.diff-lines {
  padding: 4px 0 12px;
  font-size: var(--ui-font-size);
  line-height: 1.5;
  -webkit-overflow-scrolling: touch;
  /* Grow to the longest line so every row can fill one uniform width — this
     keeps add/del backgrounds continuous across the whole horizontal scroll. */
  width: max-content;
  min-width: 100%;
}

.dl {
  display: flex;
  align-items: flex-start;
  min-height: 18px;
  white-space: pre;
  /* Fill the (uniform) width of .diff-lines so the add/del background paints
     end-to-end, even for a short line sitting next to a long one. */
  width: 100%;
}

.dl-gutter {
  flex: none;
  width: 40px;
  padding: 0 6px;
  text-align: right;
  color: var(--faint, #aeb4bc);
  background: var(--panel, #fafbfc);
  user-select: none;
  border-right: 1px solid var(--line2, #eef1f4);
  font-variant-numeric: tabular-nums;
}

.dl-gutter.new { border-right: 1px solid var(--line, #e7eaee); }

.dl-sign {
  flex: none;
  width: 16px;
  text-align: center;
  color: var(--muted);
  user-select: none;
}

.dl-text {
  /* Do not shrink: the container is sized to the longest line (see .diff-lines
     width: max-content), so the text keeps its full width and rows line up. */
  flex: none;
  padding-right: 14px;
  white-space: pre;
  color: var(--color-text);
}

/* Added / removed lines: a faint background plus a left accent bar mark the
   change, while the code TEXT keeps the normal ink colour. Washing the whole
   line in green/red competed with reading the code itself; the sign (+/-) and
   the accent carry the colour so the content stays legible. */
.dl-add {
  background: var(--color-success-soft);
  box-shadow: inset 2px 0 0 color-mix(in srgb, var(--color-success) 55%, transparent);
}
.dl-add .dl-sign {
  color: var(--color-success);
}

.dl-del {
  background: var(--color-danger-soft);
  box-shadow: inset 2px 0 0 color-mix(in srgb, var(--color-danger) 55%, transparent);
}
.dl-del .dl-sign {
  color: var(--color-danger);
}

/* Hunk header — muted band spanning the whole row. */
.dl-hunk {
  background: var(--panel2, #f3f5f8);
}
.dl-hunk .hunk-text,
.dl-omission .hunk-text {
  flex: 1;
  padding: 1px 12px;
  color: var(--muted, #8b929b);
  font-style: normal;
}
.dl-omission {
  background: var(--color-surface-raised);
  font-style: italic;
}

@media (max-width: 640px) {
  .diff-lines {
    overflow-x: auto;
    font-size: var(--ui-font-size);
  }
}
</style>
