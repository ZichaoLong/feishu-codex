import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRenderer, defineComponent, nextTick, ref } from 'vue';
import { useComposerDraft } from '../src/composables/useComposerDraft';
import { draftStorageKey } from '../src/lib/storage';

function memoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => {
      map.clear();
    },
    getItem: (key: string) => map.get(key) ?? null,
    key: (index: number) => Array.from(map.keys())[index] ?? null,
    removeItem: (key: string) => {
      map.delete(key);
    },
    setItem: (key: string, value: string) => {
      map.set(key, value);
    },
  };
}

function setup(initialSid: string | undefined) {
  const sid = ref(initialSid);
  const draft = useComposerDraft({ sessionId: () => sid.value });
  return {
    draft,
    text: draft.text,
    setSid: (next: string | undefined) => {
      sid.value = next;
    },
  };
}

function mountedSetup(initialSid: string | undefined) {
  const sid = ref(initialSid);
  let draft: ReturnType<typeof useComposerDraft> | null = null;
  const renderer = createRenderer<Record<string, never>, Record<string, never>>({
    patchProp: () => undefined,
    insert: () => undefined,
    remove: () => undefined,
    createElement: () => ({}),
    createText: () => ({}),
    createComment: () => ({}),
    setText: () => undefined,
    setElementText: () => undefined,
    parentNode: () => null,
    nextSibling: () => null,
    querySelector: () => null,
    setScopeId: () => undefined,
    insertStaticContent: () => [{}, {}],
  });
  const app = renderer.createApp(defineComponent({
    setup() {
      draft = useComposerDraft({ sessionId: () => sid.value });
      return () => null;
    },
  }));
  app.mount({});
  if (draft === null) throw new Error('mounted draft owner was not created');
  return { app, draft: draft as ReturnType<typeof useComposerDraft>, sid };
}

describe('useComposerDraft', () => {
  let original: Storage | undefined;

  beforeEach(() => {
    vi.useFakeTimers();
    original = (globalThis as { localStorage?: Storage }).localStorage;
    Object.defineProperty(globalThis, 'localStorage', {
      value: memoryStorage(),
      configurable: true,
      writable: true,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    if (original === undefined) {
      delete (globalThis as { localStorage?: Storage }).localStorage;
    } else {
      Object.defineProperty(globalThis, 'localStorage', {
        value: original,
        configurable: true,
        writable: true,
      });
    }
    vi.unstubAllGlobals();
  });

  it('loads the stored draft for the session on init', () => {
    globalThis.localStorage.setItem(draftStorageKey('s1'), 'saved draft');
    const { text } = setup('s1');
    expect(text.value).toBe('saved draft');
  });

  it('starts empty when the session has no stored draft', () => {
    const { text } = setup('s1');
    expect(text.value).toBe('');
  });

  it('persists the draft when the text changes', async () => {
    const { text } = setup('s1');
    text.value = 'hello';
    await nextTick();
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBeNull();
    await vi.runAllTimersAsync();
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBe('hello');
  });

  it('clears the stored draft when the text is emptied', async () => {
    globalThis.localStorage.setItem(draftStorageKey('s1'), 'x');
    const { text } = setup('s1');
    text.value = '';
    await nextTick();
    await vi.runAllTimersAsync();
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBeNull();
  });

  it('saves the old draft and loads the new one on session switch', async () => {
    const { text, setSid } = setup('s1');
    text.value = 'draft-s1';
    await nextTick();
    globalThis.localStorage.setItem(draftStorageKey('s2'), 'draft-s2');

    setSid('s2');
    await nextTick();

    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBe('draft-s1');
    expect(text.value).toBe('draft-s2');
  });

  it('does not rewrite the restored draft or duplicate the old-session flush', async () => {
    globalThis.localStorage.setItem(draftStorageKey('s2'), 'draft-s2');
    const storageSpy = vi.spyOn(globalThis.localStorage, 'setItem');
    const { text, setSid } = setup('s1');
    text.value = 'draft-s1';

    setSid('s2');
    await vi.runAllTimersAsync();

    expect(storageSpy).toHaveBeenCalledTimes(1);
    expect(storageSpy).toHaveBeenCalledWith(draftStorageKey('s1'), 'draft-s1');
    expect(text.value).toBe('draft-s2');
  });

  it('keeps rapid A to B to A switches inside their captured draft scopes', async () => {
    globalThis.localStorage.setItem(draftStorageKey('s1'), 'stored-s1');
    globalThis.localStorage.setItem(draftStorageKey('s2'), 'stored-s2');
    const { text, setSid } = setup('s1');

    text.value = 'latest-s1';
    setSid('s2');
    expect(text.value).toBe('stored-s2');
    text.value = 'latest-s2';
    setSid('s1');
    await vi.runAllTimersAsync();

    expect(text.value).toBe('latest-s1');
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBe('latest-s1');
    expect(globalThis.localStorage.getItem(draftStorageKey('s2'))).toBe('latest-s2');
  });

  it('coalesces typing writes and saves the exact final draft after 250ms', async () => {
    const storageSpy = vi.spyOn(globalThis.localStorage, 'setItem');
    const { text } = setup('s1');

    text.value = 'h';
    await nextTick();
    text.value = 'he';
    await nextTick();
    text.value = 'hello';
    await nextTick();

    expect(storageSpy).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(249);
    expect(storageSpy).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(storageSpy).toHaveBeenCalledTimes(1);
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBe('hello');
  });

  it('flushes the latest draft from delivered visibility, pagehide, and unmount hooks', () => {
    const lifecycleWindow = new EventTarget();
    const lifecycleDocument = new EventTarget();
    let visibilityState: DocumentVisibilityState = 'visible';
    Object.defineProperty(lifecycleDocument, 'visibilityState', {
      configurable: true,
      get: () => visibilityState,
    });
    vi.stubGlobal('window', lifecycleWindow);
    vi.stubGlobal('document', lifecycleDocument);
    const { app, draft } = mountedSetup('s1');

    draft.text.value = 'still visible';
    lifecycleDocument.dispatchEvent(new Event('visibilitychange'));
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBeNull();

    visibilityState = 'hidden';
    lifecycleDocument.dispatchEvent(new Event('visibilitychange'));
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBe(
      'still visible',
    );

    draft.text.value = 'visible page draft';
    lifecycleWindow.dispatchEvent(new Event('pagehide'));
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBe(
      'visible page draft',
    );

    draft.text.value = 'latest before unmount';
    app.unmount();
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBe(
      'latest before unmount',
    );

    draft.text.value = 'after unmount';
    lifecycleWindow.dispatchEvent(new Event('pagehide'));
    lifecycleDocument.dispatchEvent(new Event('visibilitychange'));
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBe(
      'latest before unmount',
    );
  });

  it('loadForEdit replaces the text', () => {
    const { draft } = setup('s1');
    draft.loadForEdit('edit me');
    expect(draft.text.value).toBe('edit me');
  });

  it('autosize fits the textarea height to its content', () => {
    const { draft } = setup('s1');
    const style: Record<string, string> = {};
    const el = { scrollHeight: 120, style };
    draft.textareaRef.value = el as unknown as HTMLTextAreaElement;

    draft.autosize();
    expect(style.height).toBe('120px');
  });

  it('autosize shrinks the textarea when content is removed', () => {
    const { draft } = setup('s1');
    const style: Record<string, string> = {};
    const el = { scrollHeight: 120, style };
    draft.textareaRef.value = el as unknown as HTMLTextAreaElement;

    draft.autosize();
    el.scrollHeight = 40;
    draft.autosize();
    expect(style.height).toBe('40px');
  });

  it('autosize is a no-op before the textarea mounts', () => {
    const { draft } = setup('s1');
    expect(() => {
      draft.autosize();
    }).not.toThrow();
  });

  it('clearDraft removes the persisted draft synchronously', async () => {
    // Regression: when the first message of an empty session is submitted, the
    // optimistic user turn unmounts the composer before the post-flush text
    // watcher can clear the draft. clearDraft must therefore clear it
    // synchronously so a remount does not reload the stale text.
    globalThis.localStorage.setItem(draftStorageKey('s1'), 'stale draft');
    const { draft } = setup('s1');
    draft.text.value = 'pending text that was submitted';
    draft.clearDraft();
    // No nextTick — the write is synchronous.
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBeNull();

    // Simulate the remount after the optimistic turn: a fresh composable
    // instance for the same session should start empty, not restore the draft.
    const { text } = setup('s1');
    expect(text.value).toBe('');
    await vi.runAllTimersAsync();
    expect(globalThis.localStorage.getItem(draftStorageKey('s1'))).toBeNull();
  });
});
