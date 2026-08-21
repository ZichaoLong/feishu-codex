<!-- apps/kimi-web/src/components/chat/ConversationToc.vue -->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { ChatTurn } from '../../types';
import Button from '../ui/Button.vue';
import Dialog from '../ui/Dialog.vue';
import Icon from '../ui/Icon.vue';
import Menu from '../ui/Menu.vue';
import MenuItem from '../ui/MenuItem.vue';

export interface ConversationTocItem {
  id: string;
  role: ChatTurn['role'];
  no: number;
  title: string;
}

const props = defineProps<{
  items: ConversationTocItem[];
  /** Query currently owning the viewport middle. */
  activeTurnId: string | null;
  mobile?: boolean;
  sessionLoading?: boolean;
  /** The external outline stopped at its explicit prompt limit. */
  truncated?: boolean;
  /** More summary-only outline pages are available on explicit demand. */
  hasMore?: boolean;
  loadingMore?: boolean;
  /** Keep search discoverable for a selected thread, including unavailable modes. */
  searchVisible?: boolean;
  /** Resolve and reveal a target through the pane's single navigation path. */
  selectTarget?: (turnId: string) => Promise<boolean>;
  /** Temporarily hidden while a wide table actually covers the rail. Kept out
      of `railVisible` on purpose: the nav must stay mounted so the occlusion can
      be measured and lifted again. Never touches the user's TOC setting. */
  occluded?: boolean;
}>();

const emit = defineEmits<{
  select: [turnId: string];
  loadMore: [];
  search: [];
}>();

const { t } = useI18n();

// Width the rail needs beside the reading column once its labels are fully
// revealed on hover/focus: 3px bar + 10px gap + 220px label, plus a small
// buffer so the text never kisses the container edge. Kept in sync with the
// `.toc-bar` / `.toc-label` rules below.
const EXPANDED_WIDTH = 240;

const navRef = ref<HTMLElement | null>(null);
const compactOpen = ref(false);
// Whether the rail, once expanded, fits within the room to the right of the
// reading column. When it would overflow, we hide the outline entirely rather
// than showing a panel that gets clipped by the container edge.
const fits = ref(true);
let selectionGeneration = 0;

let observer: ResizeObserver | null = null;

function measure(): void {
  const nav = navRef.value;
  const parent = nav?.offsetParent as HTMLElement | null;
  if (!nav || !parent) return;
  const navLeft = nav.getBoundingClientRect().left;
  const parentRight = parent.getBoundingClientRect().right;
  fits.value = parentRight - navLeft >= EXPANDED_WIDTH;
}

// The outline is only useful once there is something to navigate, and it never
// shows on mobile or while the session is still loading. `fits` is kept out of
// this computed so the nav stays mounted (and measurable) even when hidden;
// clipping is applied via the `toc-clipped` class instead.
const railVisible = computed(
  () => !props.mobile
    && !props.sessionLoading
    && (props.items.length > 1 || props.searchVisible === true),
);
const compactVisible = computed(
  () => !props.sessionLoading
    && (props.items.length > 0 || props.searchVisible === true)
    && (props.mobile || !fits.value),
);

async function selectItem(turnId: string): Promise<void> {
  const generation = ++selectionGeneration;
  let installed = true;
  if (props.selectTarget) installed = await props.selectTarget(turnId);
  else emit('select', turnId);
  if (installed && generation === selectionGeneration) compactOpen.value = false;
}

function openSearch(): void {
  selectionGeneration += 1;
  compactOpen.value = false;
  emit('search');
}

// The nav is rendered only while `railVisible` (v-if), so a mount while navRef is
// still null (during sessionLoading, on mobile, or before a second user turn)
// would skip the ResizeObserver setup and leave `fits` at its default `true`.
// Re-initialize whenever the nav is actually rendered so `fits` is measured
// against the real layout instead.
watch(
  railVisible,
  (isVisible) => {
    observer?.disconnect();
    observer = null;
    if (!isVisible) return;
    void nextTick(() => {
      const nav = navRef.value;
      const parent = nav?.offsetParent as HTMLElement | null;
      if (!nav || !parent) return;
      if (typeof ResizeObserver !== 'undefined') {
        observer = new ResizeObserver(measure);
        observer.observe(parent);
      }
      measure();
    });
  },
  { immediate: true },
);

watch(
  compactVisible,
  (isVisible) => {
    if (!isVisible) compactOpen.value = false;
  },
  { immediate: true },
);

// A historical resolver may outlive an Esc/overlay/close-button dismissal.
// Invalidate its UI receipt synchronously so reopening the dialog cannot be
// closed later by that older success.
watch(
  compactOpen,
  (isOpen, wasOpen) => {
    if (wasOpen && !isOpen) selectionGeneration += 1;
  },
  { flush: 'sync' },
);

onBeforeUnmount(() => {
  observer?.disconnect();
  observer = null;
});
</script>

<template>
  <!-- Conversation outline: a vertical list of short bars (one per user query),
       vertically centered beside the chat. Hovering the list enlarges the bars
       and reveals each query's title to the right, making rows easy to click. -->
  <nav
    v-if="railVisible"
    ref="navRef"
    class="conversation-toc"
    :class="{ 'toc-clipped': !fits || occluded }"
    :aria-label="t('conversation.toc')"
    :aria-hidden="fits && !occluded ? undefined : true"
  >
    <div class="toc-scroll">
      <button
        v-for="item in items"
        :key="item.id"
        type="button"
        class="toc-row"
        :class="{ active: activeTurnId === item.id }"
        @click="selectItem(item.id)"
      >
        <span class="toc-bar" />
        <span class="toc-label">{{ item.title }}</span>
      </button>
    </div>
    <p v-if="truncated" class="toc-truncated">
      {{ t('conversation.tocTruncated') }}
    </p>
    <button
      v-else-if="hasMore"
      type="button"
      class="toc-more"
      :disabled="loadingMore"
      @click="emit('loadMore')"
    >
      {{ loadingMore ? t('conversation.loadingOlder') : t('conversation.loadMoreOutline') }}
    </button>
    <button
      v-if="searchVisible"
      type="button"
      class="toc-search"
      :aria-label="t('conversation.searchConversation')"
      :title="t('conversation.searchConversation')"
      @click="openSearch"
    >
      <Icon name="search" size="sm" />
      <span>{{ t('conversation.searchConversation') }}</span>
    </button>
  </nav>

  <Button
    v-if="compactVisible"
    class="toc-compact-trigger"
    :class="{ 'is-mobile': mobile }"
    variant="secondary"
    size="sm"
    aria-haspopup="dialog"
    :aria-expanded="compactOpen"
    @click="compactOpen = true"
  >
    {{ t('conversation.promptHistory') }}
  </Button>

  <Dialog
    v-model:open="compactOpen"
    :title="t('conversation.promptHistory')"
    :padded="false"
    size="md"
  >
    <Menu class="toc-compact-list" :aria-label="t('conversation.toc')">
      <MenuItem
        v-if="searchVisible"
        :size="mobile ? 'lg' : 'md'"
        @click="openSearch"
      >
        <Icon name="search" size="sm" />
        <span>{{ t('conversation.searchConversation') }}</span>
      </MenuItem>
      <MenuItem
        v-for="item in items"
        :key="item.id"
        :active="activeTurnId === item.id"
        :size="mobile ? 'lg' : 'md'"
        @click="selectItem(item.id)"
      >
        <span class="toc-compact-number">{{ item.no }}</span>
        <span class="toc-compact-title">{{ item.title }}</span>
      </MenuItem>
    </Menu>
    <div v-if="truncated || hasMore" class="toc-compact-footer">
      <p v-if="truncated" class="toc-compact-truncated">
        {{ t('conversation.tocTruncated') }}
      </p>
      <Button
        v-else-if="hasMore"
        variant="ghost"
        size="sm"
        :loading="loadingMore"
        @click="emit('loadMore')"
      >
        {{ t('conversation.loadMoreOutline') }}
      </Button>
    </div>
  </Dialog>
</template>

<style scoped>
.conversation-toc {
  position: absolute;
  z-index: var(--z-sticky);
  top: 50%;
  transform: translateY(-50%);
  /* Anchor to the reading-column edge, the rail's original position. Tables
     that grow past it (up to --p-table-max) temporarily hide the rail via the
     occlusion hit-test in ConversationPane, so proximity is safe again.
     The cqi cap keeps the rail inside narrow containers. */
  --toc-content-max: min(
    var(--p-content-max),
    calc(100cqi - var(--space-5) - var(--space-5))
  );
  left: calc(50% + (var(--toc-content-max) / 2) + 14px);
  display: flex;
  flex-direction: column;
  justify-content: center;
  opacity: 0.5;
  transition: opacity var(--duration-base) var(--ease-out);
}
/* Invisible hover bridge: the collapsed rail is only a few px wide, so this
   extends the hover target on both sides to make the outline easy to open and
   forgiving to stay within. The left side covers only the 14px gap to the
   content edge — a table wide enough to reach past the gap also covers the
   bar, which hides the rail (pointer-events: none) before the bridge can
   steal its events. Kept at z-index 0 so it sits behind the rows (which are
   raised to z-index 1) — otherwise the bridge, as a positioned pseudo-element,
   paints above the in-flow rows and swallows their clicks. */
.conversation-toc::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: -14px;
  right: -48px;
  z-index: 0;
}
.conversation-toc:hover,
.conversation-toc:focus-within { opacity: 1; }

.toc-scroll {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 8px 0;
  max-height: calc(100vh - 200px);
  overflow-y: auto;
  scrollbar-width: none;
}
.toc-scroll::-webkit-scrollbar { display: none; }

.toc-truncated {
  max-width: 220px;
  margin: 4px 0 0 13px;
  color: var(--color-text-muted);
  font-family: var(--font-ui);
  font-size: var(--text-xs);
  line-height: 1.35;
  white-space: nowrap;
}
.toc-more {
  position: relative;
  z-index: 1;
  max-width: 220px;
  margin: 4px 0 0 13px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--color-accent);
  font: var(--text-xs) var(--font-ui);
  text-align: left;
  cursor: pointer;
}
.toc-more:disabled { color: var(--color-text-muted); cursor: default; }
.toc-search {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  width: 220px;
  margin: 6px 0 0 -5px;
  padding: 5px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-muted);
  font: var(--text-sm) var(--font-ui);
  text-align: left;
  cursor: pointer;
}
.toc-search span {
  opacity: 0;
  transition: opacity var(--duration-fast) var(--ease-out);
}
.conversation-toc:hover .toc-search span,
.conversation-toc:focus-within .toc-search span { opacity: 1; }
.toc-search:hover { background: var(--color-surface-sunken); color: var(--color-text); }
.toc-search:focus-visible { outline: none; box-shadow: var(--p-focus-ring); }

.toc-row {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 18px;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--color-text-muted);
  font-family: var(--font-ui);
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
  white-space: nowrap;
}
.toc-row:focus-visible { outline: none; box-shadow: var(--p-focus-ring); }

.toc-bar {
  flex: none;
  width: 3px;
  height: 14px;
  border-radius: var(--radius-full);
  background: var(--color-accent);
  opacity: 0.3;
  transition:
    opacity var(--duration-fast) var(--ease-out),
    height var(--duration-fast) var(--ease-out);
}
.toc-label {
  display: block;
  max-width: 0;
  overflow: hidden;
  opacity: 0;
  text-overflow: ellipsis;
  transition:
    max-width 220ms var(--ease-out),
    opacity var(--duration-fast) var(--ease-out),
    color var(--duration-fast) var(--ease-out);
}

/* Hover / focus: enlarge bars and reveal labels to the right. */
.conversation-toc:hover .toc-bar,
.conversation-toc:focus-within .toc-bar { height: 18px; opacity: 0.5; }
.conversation-toc:hover .toc-label,
.conversation-toc:focus-within .toc-label { max-width: 220px; opacity: 1; }

.toc-row.active .toc-bar { opacity: 1; height: 18px; }
.toc-row.active .toc-label { color: var(--color-accent); font-weight: var(--weight-medium); }
.toc-row:hover .toc-bar { opacity: 1; }
.toc-row:hover .toc-label { color: var(--color-text); }

/* When there is not enough room to the right of the reading column to reveal
   the labels, the rail is kept mounted (so its position can keep being
   measured) but hidden from view and from pointer/screen-reader interaction. */
.conversation-toc.toc-clipped {
  visibility: hidden;
  pointer-events: none;
}

.toc-compact-trigger {
  position: absolute;
  z-index: var(--z-sticky);
  top: 64px;
  right: var(--space-4);
  max-width: calc(100% - var(--space-8));
}
.toc-compact-trigger.is-mobile { top: var(--space-3); }

.toc-compact-list {
  width: 100%;
  max-height: min(60vh, 520px);
  overflow-y: auto;
  padding: var(--space-2);
  border: 0;
  border-radius: 0;
  box-shadow: none;
}
.toc-compact-list :deep(.ui-menu-item) { min-width: 0; }
.toc-compact-number {
  flex: none;
  width: 3ch;
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.toc-compact-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.toc-compact-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  min-height: 48px;
  padding: var(--space-2) var(--space-3);
  border-top: 1px solid var(--color-line);
}
.toc-compact-truncated {
  margin: 0;
  color: var(--color-text-muted);
  font-family: var(--font-ui);
  font-size: var(--text-xs);
}
</style>
