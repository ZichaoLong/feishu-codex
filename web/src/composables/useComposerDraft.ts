// apps/kimi-web/src/composables/useComposerDraft.ts
import {
  getCurrentInstance,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from 'vue';
import { draftStorageKey, safeGetString, safeRemove, safeSetString } from '../lib/storage';

export interface ComposerDraftDeps {
  /** Active session id — scopes the persisted draft (getter for reactivity). */
  sessionId: () => string | undefined;
}

export const COMPOSER_DRAFT_SAVE_DELAY_MS = 250;

/**
 * The composer's text state plus its per-session unsent-draft persistence.
 *
 * The draft is kept in localStorage keyed by session, so switching away and back
 * (or a page refresh) restores whatever the user was typing for that session; it
 * is cleared when the draft is sent/steered. This composable owns the `text`
 * and `textarea` refs, the `autosize` helper, the draft load/save watchers, and
 * the imperative `loadForEdit` handle exposed to the parent.
 */
export function useComposerDraft(deps: ComposerDraftDeps) {
  const { sessionId } = deps;

  function loadDraft(sid: string | undefined): string {
    return safeGetString(draftStorageKey(sid)) ?? '';
  }
  function saveDraft(sid: string | undefined, value: string): void {
    const key = draftStorageKey(sid);
    if (value) safeSetString(key, value);
    else safeRemove(key);
  }

  const text = ref(loadDraft(sessionId()));
  const textareaRef = ref<HTMLTextAreaElement | null>(null);
  let pendingSave: { sessionId: string | undefined; value: string } | null = null;
  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  let restoringSessionDraft = false;

  function flushDraft(): void {
    if (saveTimer !== null) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    const pending = pendingSave;
    pendingSave = null;
    if (pending) saveDraft(pending.sessionId, pending.value);
  }

  function scheduleDraftSave(value: string): void {
    pendingSave = { sessionId: sessionId(), value };
    if (saveTimer !== null) clearTimeout(saveTimer);
    saveTimer = setTimeout(flushDraft, COMPOSER_DRAFT_SAVE_DELAY_MS);
  }

  function autosize(): void {
    const el = textareaRef.value;
    if (!el) return;
    // Reset to measure the natural content height, then fit the box to it.
    // The resting height and the upper cap live in CSS (`min-height` /
    // `max-height`); once the content outgrows the cap, `overflow-y: auto`
    // scrolls internally. This keeps a single source of truth for the bounds.
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }

  watch(
    text,
    (value) => {
      void nextTick(autosize);
      // Storage is synchronous in every browser. Capture the latest value in
      // the same input tick, then coalesce ordinary typing so it cannot block
      // one main-thread write per character. Lifecycle boundaries below can
      // therefore flush the exact latest value even before Vue's next tick.
      if (!restoringSessionDraft) scheduleDraftSave(value);
    },
    { flush: 'sync' },
  );

  // Switching sessions: stash the draft under the OLD session, then load the new
  // session's draft into the box.
  watch(
    sessionId,
    (newSid, oldSid) => {
      if (newSid === oldSid) return;
      flushDraft();
      restoringSessionDraft = true;
      text.value = loadDraft(newSid);
      restoringSessionDraft = false;
      void nextTick(autosize);
    },
    { flush: 'sync' },
  );

  /** Imperatively load text into the box for editing (used when a prior message
      is copied into an unsent follow-up draft, or when a queued prompt is edited).
      Focuses with the caret at the end. */
  function loadForEdit(value: string): void {
    text.value = value;
    void nextTick(() => {
      const el = textareaRef.value;
      if (!el) return;
      el.focus();
      const pos = value.length;
      el.setSelectionRange(pos, pos);
      autosize();
    });
  }

  /**
   * Synchronously clear the persisted draft for the current session.
   * Call this right after clearing `text.value` on send/steer; relying on the
   * text watcher is unsafe because the Composer may unmount before the watcher
   * flushes (e.g. when the optimistic user message replaces the empty-session
   * composer), causing the next mount to reload the stale draft.
   */
  function clearDraft(): void {
    pendingSave = null;
    if (saveTimer !== null) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    saveDraft(sessionId(), '');
  }

  function onPageHide(): void {
    flushDraft();
  }

  function onVisibilityChange(): void {
    if (document.visibilityState === 'hidden') flushDraft();
  }

  // The composable is also exercised as a plain state owner in unit tests.
  // Register browser lifecycle hooks only when it actually belongs to a Vue
  // component instance.
  if (getCurrentInstance()) {
    onMounted(() => {
      window.addEventListener('pagehide', onPageHide);
      document.addEventListener('visibilitychange', onVisibilityChange);
    });
    onBeforeUnmount(() => {
      flushDraft();
      window.removeEventListener('pagehide', onPageHide);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    });
  }

  return { text, textareaRef, autosize, loadForEdit, clearDraft };
}
