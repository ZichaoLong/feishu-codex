<script lang="ts">
import type { DiffViewLine } from '../types';
import type { FocusFileChangeSourceKind } from './types';

const UNIFIED_HUNK_RE = /^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/u;

function sourceLines(source: string): string[] {
  if (source === '') return [];
  const lines = source.split(/\r\n|\n|\r/u);
  if (lines.at(-1) === '') lines.pop();
  return lines;
}

/** Parse every row of an update's unified diff without a presentation bound. */
export function parseCompleteUnifiedDiff(source: string): DiffViewLine[] {
  const lines: DiffViewLine[] = [];
  let oldNo = 0;
  let newNo = 0;
  let inHunk = false;

  for (const rawLine of sourceLines(source)) {
    const match = UNIFIED_HUNK_RE.exec(rawLine);
    if (match) {
      const nextOldNo = Number(match[1]);
      const nextNewNo = Number(match[2]);
      if (Number.isSafeInteger(nextOldNo) && Number.isSafeInteger(nextNewNo)) {
        oldNo = nextOldNo;
        newNo = nextNewNo;
        inHunk = true;
      } else {
        inHunk = false;
      }
      lines.push({ type: 'hunk', text: rawLine });
      continue;
    }
    if (
      !inHunk
      || rawLine.startsWith('diff --git ')
      || rawLine.startsWith('index ')
      || rawLine.startsWith('--- ')
      || rawLine.startsWith('+++ ')
    ) {
      lines.push({ type: 'hunk', text: rawLine });
      continue;
    }
    if (rawLine.startsWith('+')) {
      lines.push({ type: 'add', text: rawLine.slice(1), newNo });
      newNo += 1;
      continue;
    }
    if (rawLine.startsWith('-')) {
      lines.push({ type: 'del', text: rawLine.slice(1), oldNo });
      oldNo += 1;
      continue;
    }
    if (rawLine.startsWith(' ')) {
      lines.push({
        type: 'context',
        text: rawLine.slice(1),
        oldNo,
        newNo,
      });
      oldNo += 1;
      newNo += 1;
      continue;
    }
    lines.push({ type: 'hunk', text: rawLine });
  }
  return lines;
}

/**
 * Interpret an admitted FileChange for semantic presentation. Add/delete carry
 * complete file text, while update carries a unified diff. The source string
 * remains the copy payload; this parser never crops it.
 */
export function parseCompleteFileChangeDiff(
  source: string,
  kind: FocusFileChangeSourceKind,
): DiffViewLine[] {
  if (kind.type === 'update') return parseCompleteUnifiedDiff(source);
  return sourceLines(source).map((text, index) => (
    kind.type === 'add'
      ? { type: 'add', text, newNo: index + 1 }
      : { type: 'del', text, oldNo: index + 1 }
  ));
}
</script>

<script setup lang="ts">
import { computed } from 'vue';
import DiffLines from '../components/chat/DiffLines.vue';

const props = defineProps<{
  source: string;
  kind: FocusFileChangeSourceKind;
}>();
const lines = computed(() => parseCompleteFileChangeDiff(props.source, props.kind));
</script>

<template>
  <div class="focus-complete-diff">
    <DiffLines :lines="lines" mode="complete" />
  </div>
</template>

<style scoped>
.focus-complete-diff {
  min-width: 0;
  overflow: auto;
  color: var(--color-text);
  background: var(--color-surface-sunken);
  font-family: var(--font-mono);
}
</style>
