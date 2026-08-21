<!-- apps/kimi-web/src/components/chat/AuthMedia.vue
     Renders a controlled user-uploaded image whose bytes require authenticated
     loading. Focus intentionally does not use this component as a media
     player for video/audio attachments. -->
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { loadAuthenticatedMediaBlob, type AuthenticatedMediaLoader } from '../../lib/authenticatedMedia';

const props = withDefaults(
  defineProps<{
    url: string;
    kind: 'image' | 'video';
    alt?: string;
    /** File-store id. When present the bytes are fetched with auth and played
     *  from a blob URL; otherwise `url` is used directly (e.g. a data: URL). */
    fileId?: string;
    /** Optional product-specific authenticated byte loader. Without one the
     *  component keeps the supplied URL and never discovers a transport. */
    loadBlob?: AuthenticatedMediaLoader;
    mediaClass?: string;
    /** Retained for Kimi-derived call sites; Focus renders images only. */
    controls?: boolean;
    /** Retained for Kimi-derived call sites; Focus renders images only. */
    muted?: boolean;
  }>(),
  { mediaClass: 'u-img', controls: true, muted: false },
);

const resolvedUrl = ref<string>(props.fileId ? '' : props.url);
const mediaEl = ref<HTMLElement | null>(null);
// Flips true once the element nears the viewport, deferring the authenticated
// download so a session with many historical large uploads doesn't fetch every
// blob (and hold them in memory) before the user ever scrolls to or plays them.
const visible = ref(!props.fileId);
let objectUrl: string | null = null;
// Sequence guard + unmount flag: a reused component (e.g. queued thumbnails
// keyed by index) can change fileId before a previous fetch resolves, and an
// in-flight fetch can outlive the component. In both cases the stale response
// must not win or leak its blob URL.
let requestSeq = 0;
let disposed = false;
let observer: IntersectionObserver | null = null;

function revoke(): void {
  if (objectUrl !== null) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
}

async function resolve(): Promise<void> {
  const seq = ++requestSeq;
  revoke();
  if (!props.fileId) {
    resolvedUrl.value = props.url;
    return;
  }
  if (!visible.value) return; // defer until near the viewport
  try {
    const blob = await loadAuthenticatedMediaBlob(props.fileId, props.loadBlob);
    const url = URL.createObjectURL(blob);
    if (disposed || seq !== requestSeq) {
      URL.revokeObjectURL(url);
      return;
    }
    objectUrl = url;
    resolvedUrl.value = objectUrl;
  } catch {
    if (disposed || seq !== requestSeq) return;
    // Honest broken-media state beats a blank box if the authenticated fetch fails.
    resolvedUrl.value = props.url;
  }
}

watch(() => [props.fileId, props.url, props.loadBlob, visible.value] as const, resolve, { immediate: true });

onMounted(() => {
  if (typeof IntersectionObserver === 'function' && mediaEl.value) {
    observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          visible.value = true;
          observer?.disconnect();
          observer = null;
        }
      },
      { rootMargin: '200px' },
    );
    observer.observe(mediaEl.value);
  } else {
    visible.value = true;
  }
});

onBeforeUnmount(() => {
  disposed = true;
  observer?.disconnect();
  observer = null;
  revoke();
});
</script>

<template>
  <img
    ref="mediaEl"
    :class="mediaClass"
    :src="resolvedUrl || undefined"
    :alt="alt || ''"
    loading="lazy"
  />
</template>
