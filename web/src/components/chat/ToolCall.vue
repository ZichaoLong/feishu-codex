<!-- apps/kimi-web/src/components/chat/ToolCall.vue -->
<script setup lang="ts">
import { computed } from 'vue';
import type { FilePreviewRequest, ToolCall, ToolMedia } from '../../types';
import { resolveToolRenderer } from './tool-calls/toolRegistry';

const props = withDefaults(
  defineProps<{
    tool: ToolCall;
    mobile?: boolean;
    stackPosition?: 'single' | 'first' | 'middle' | 'last';
    toolDiffPanel?: boolean;
    toolDetailAvailable?: boolean;
  }>(),
  { mobile: false, stackPosition: 'single', toolDiffPanel: false },
);

const emit = defineEmits<{
  openMedia: [media: ToolMedia];
  openFile: [target: FilePreviewRequest];
  openToolDiff: [tool: ToolCall];
  openAgent: [toolCallId: string];
}>();

const Renderer = computed(() => resolveToolRenderer(props.tool));
const rendererToolDetailAvailable = computed(() => (
  props.tool.inspectionLocator?.kind === 'commandExecution'
  || props.tool.inspectionLocator?.kind === 'fileChange'
    ? props.toolDetailAvailable
    : undefined
));
</script>

<template>
  <component
    :is="Renderer"
    :tool="tool"
    :mobile="mobile"
    :stack-position="stackPosition"
    :tool-diff-panel="toolDiffPanel"
    :tool-detail-available="rendererToolDetailAvailable"
    :data-scroll-anchor-id="tool.id"
    @open-media="emit('openMedia', $event)"
    @open-file="emit('openFile', $event)"
    @open-tool-diff="emit('openToolDiff', tool)"
    @open-agent="emit('openAgent', $event)"
  />
</template>
