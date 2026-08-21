// apps/kimi-web/src/composables/useAttachmentUpload.ts
// Attachment handling for the composer: file picker, paste, drag & drop, the
// upload machinery, the chip strip, and the preview lightbox. Images and
// Only verified images get a browser thumbnail and lightbox. Every other MIME
// type, including video and audio, is an ordinary file chip (icon + name, no
// thumbnail) and is sent as a file part.
//
// Pending attachments are scoped per session (keyed by session id) so switching
// sessions can't leak one session's unsent attachments into another session's
// next submit. The composer keeps `handleSubmit` (which reads the
// attachments to build the payload) and the `hasUpload` toolbar flag; this
// composable owns the attachment state, all the file-input UI handlers, and the
// paste listener + object-URL cleanup lifecycle.

import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

export interface Attachment {
  /** Unique local id (used as :key) */
  localId: string;
  /** File name */
  name: string;
  /** Only image receives a browser preview; legacy video input is normalized to file. */
  kind: 'image' | 'video' | 'file';
  /** Controlled image URL or an accepted-upload thumbnail (unset for files). */
  previewUrl?: string;
  /** Local MIME of the picked file — echoed into the wire file part. */
  mediaType?: string;
  /** Local byte size of the picked file — echoed into the wire file part. */
  size?: number;
  /** True while uploading */
  uploading: boolean;
  /** Resolved daemon file id (set after upload completes) */
  fileId?: string;
  /** True if upload failed */
  error?: boolean;
}

export interface UploadedAttachment {
  fileId: string;
  name: string;
  mediaType: string;
  /** A server-authorized URL for a signature-checked image, when available. */
  previewUrl?: string;
  /** False means an image can render only through previewUrl, never a local Blob. */
  localImagePreviewAllowed?: boolean;
}

type UploadImage = (
  file: Blob,
  name?: string,
) => Promise<UploadedAttachment | null>;

const VERIFIED_IMAGE_MEDIA_TYPES = new Set([
  'image/avif',
  'image/bmp',
  'image/gif',
  'image/jpeg',
  'image/png',
  'image/webp',
  'image/x-icon',
]);

export interface AttachmentUploadDeps {
  /** Upload a blob; resolves to the daemon file id, or null on failure.
      Getter so a prop change is picked up. Undefined disables attaching. */
  uploadImage: () => UploadImage | undefined;
  /** Fetch an already-uploaded file for authenticated previews. Undefined
      keeps the server URL as an honest fallback and never discovers another
      product transport implicitly. */
  downloadFile?: () => ((fileId: string) => Promise<Blob>) | undefined;
  /** Active session id — scopes pending attachments (getter for reactivity). */
  sessionId: () => string | undefined;
}

interface ParsedAttachmentSessionId {
  semanticKey: string;
  generation: number;
}

function parseAttachmentSessionId(sessionId: string): ParsedAttachmentSessionId | null {
  const match = /^(.*):generation:([1-9]\d*):((?:thread|draft):.+)$/u.exec(sessionId);
  if (!match) return null;
  const generation = Number(match[2]);
  if (!Number.isSafeInteger(generation) || generation <= 0) return null;
  return {
    semanticKey: `${match[1]}\u0000${match[3]}`,
    generation,
  };
}

export function useAttachmentUpload(deps: AttachmentUploadDeps) {
  const { uploadImage, downloadFile, sessionId } = deps;

  const attachmentsBySession = ref<Record<string, Attachment[]>>({});
  const attachments = computed(() => attachmentsBySession.value[sessionId() ?? ''] ?? []);
  const previewAttachment = ref<Attachment | null>(null);
  const fileInputRef = ref<HTMLInputElement | null>(null);
  const isDragOver = ref(false);

  let localIdCounter = 0;
  function nextLocalId(): string {
    return `att_${++localIdCounter}`;
  }

  function setForSession(sid: string, next: Attachment[]): void {
    attachmentsBySession.value = { ...attachmentsBySession.value, [sid]: next };
  }

  function revokeAttachment(att: Attachment): void {
    if (att.previewUrl === undefined) return;
    try { URL.revokeObjectURL(att.previewUrl); } catch { /* ignore */ }
  }

  function attachmentKind(mime: string): 'image' | 'video' | 'file' {
    // A browser-provided MIME declaration is never enough to grant a media
    // preview. Limit the image surface to the native formats that Focus's
    // attachment store signature-checks; the upload result below supplies the
    // final, server-canonical type before any preview is created.
    const normalized = mime.split(';', 1)[0]?.trim().toLowerCase() ?? '';
    const canonical = normalized === 'image/jpg'
      ? 'image/jpeg'
      : normalized === 'image/vnd.microsoft.icon'
        ? 'image/x-icon'
        : normalized;
    if (VERIFIED_IMAGE_MEDIA_TYPES.has(canonical)) return 'image';
    // Everything else — including video, audio, and empty/unknown MIME — is
    // an ordinary attachment. Focus has no browser media-player contract.
    return 'file';
  }

  async function addFiles(files: File[]): Promise<void> {
    const upload = uploadImage();
    if (!upload) return;
    // Capture the session at upload time; async completion must update the same
    // session even if the user has since switched away.
    const sid = sessionId() ?? '';
    if (files.length === 0) return;

    for (const file of files) {
      const kind = attachmentKind(file.type);
      const localId = nextLocalId();
      const att: Attachment = {
        localId,
        name: file.name,
        kind,
        // Do not preview a locally selected blob before the upload endpoint
        // has accepted it. In Focus, a successful image response carries the
        // authenticated URL for the cache's signature-checked bytes.
        previewUrl: undefined,
        // Extensionless/unknown files report an empty MIME — normalize now so
        // the wire file part's required non-empty media_type never sees ''.
        mediaType: file.type || 'application/octet-stream',
        size: file.size,
        uploading: true,
      };
      setForSession(sid, [...(attachmentsBySession.value[sid] ?? []), att]);

      // Upload in background; update the attachment when done.
      upload(file, file.name).then((result) => {
        const current = attachmentsBySession.value[sid] ?? [];
        const existing = current.find((a) => a.localId === localId);
        if (!existing) return;
        const resolvedKind = result ? attachmentKind(result.mediaType) : 'file';
        const controlledPreviewUrl = result?.previewUrl?.trim() ?? '';
        const mayUseLocalPreview = result?.localImagePreviewAllowed !== false;
        const renderableKind = (
          result
          && resolvedKind === 'image'
          && (controlledPreviewUrl || mayUseLocalPreview)
        ) ? 'image' : 'file';
        // Kimi-derived callers that have no controlled URL retain their old
        // local-preview behavior, but only after their upload endpoint has
        // accepted the immutable File. Focus always supplies previewUrl and
        // therefore never renders the unverified local blob.
        const previewUrl = result && renderableKind === 'image'
          ? controlledPreviewUrl || URL.createObjectURL(file)
          : undefined;
        setForSession(
          sid,
          current.map((a) =>
            a.localId === localId
              ? {
                  ...a,
                  kind: renderableKind,
                  previewUrl,
                  uploading: false,
                  fileId: result?.fileId,
                  // Adopt the server-recorded MIME when available — the
                  // server's file meta is what the prompt route reads.
                  mediaType: result?.mediaType ?? a.mediaType,
                  error: result === null,
                }
              : a,
          ),
        );
      }).catch(() => {
        const current = attachmentsBySession.value[sid] ?? [];
        setForSession(
          sid,
          current.map((a) => (a.localId === localId ? { ...a, uploading: false, error: true } : a)),
        );
      });
    }
  }

  function removeAttachment(localId: string): void {
    const sid = sessionId() ?? '';
    const current = attachmentsBySession.value[sid] ?? [];
    const att = current.find((a) => a.localId === localId);
    if (previewAttachment.value?.localId === localId) previewAttachment.value = null;
    if (att) revokeAttachment(att);
    setForSession(sid, current.filter((a) => a.localId !== localId));
  }

  function openAttachmentPreview(att: Attachment): void {
    if (att.kind !== 'image') return;
    previewAttachment.value = att;
  }

  function closeAttachmentPreview(): void {
    previewAttachment.value = null;
  }

  function openFilePicker(): void {
    fileInputRef.value?.click();
  }

  function handleFileInputChange(e: Event): void {
    const input = e.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    void addFiles(files);
    // Reset so re-selecting the same file fires change again.
    input.value = '';
  }

  // Global document-level paste handler — captures Ctrl+V anywhere the composer is mounted.
  function handleDocumentPaste(e: ClipboardEvent): void {
    if (!uploadImage()) return;

    const cd = e.clipboardData;
    if (!cd) return;

    // Collect attached files from both .items and .files to cover all browsers/OS.
    const files: File[] = [];
    const seenKeys = new Set<string>();

    const addBlob = (blob: File | Blob, name: string): void => {
      const key = `${blob.size}:${blob.type}:${name}`;
      if (seenKeys.has(key)) return;
      seenKeys.add(key);
      const ext = blob.type.split('/')[1] ?? 'png';
      const safeName = name.includes('.') ? name : `paste-${Date.now()}.${ext}`;
      files.push(blob instanceof File ? blob : new File([blob], safeName, { type: blob.type }));
    };

    // From DataTransferItemList.
    for (const item of Array.from(cd.items)) {
      if (item.kind === 'file') {
        const blob = item.getAsFile();
        if (blob) addBlob(blob, blob.name || `paste-${Date.now()}.${item.type.split('/')[1] ?? 'png'}`);
      }
    }

    // From FileList (some browsers/OS put screenshots here directly).
    for (const file of Array.from(cd.files)) {
      addBlob(file, file.name);
    }

    if (files.length === 0) return; // No files — let normal text paste proceed unmodified.

    e.preventDefault();
    void addFiles(files);
  }

  // Drag-drop handlers. WindowDragDepth tracks nested dragenter/dragleave pairs
  // for the document-level listeners below (declared here so the composer
  // handlers can reset it on their own drop).
  let windowDragDepth = 0;

  function handleDragOver(e: DragEvent): void {
    if (!uploadImage()) return;
    const hasFiles = Array.from(e.dataTransfer?.items ?? []).some((item) => item.kind === 'file');
    if (!hasFiles) return;
    // Stop the document-level handler from double-counting this as a new enter.
    e.preventDefault();
    e.stopPropagation();
    isDragOver.value = true;
  }

  function handleDragLeave(): void {
    isDragOver.value = false;
  }

  function handleDrop(e: DragEvent): void {
    windowDragDepth = 0;
    isDragOver.value = false;
    if (!uploadImage()) return;
    // Stop the document-level drop handler from adding the same files twice.
    e.preventDefault();
    e.stopPropagation();
    const files = Array.from(e.dataTransfer?.files ?? []);
    void addFiles(files);
  }

  // Window-level drag & drop. Without a document-wide handler, dropping a file
  // anywhere outside the small composer box makes the browser navigate away to
  // the file. Nested dragenter/dragleave pairs fire while moving across child
  // elements, so the overlay is driven by a counter, not by single events.
  function windowDragHasFiles(e: DragEvent): boolean {
    return Array.from(e.dataTransfer?.items ?? []).some((item) => item.kind === 'file');
  }

  function handleWindowDragEnter(e: DragEvent): void {
    if (!uploadImage() || !windowDragHasFiles(e)) return;
    e.preventDefault();
    windowDragDepth += 1;
    isDragOver.value = true;
  }

  function handleWindowDragOver(e: DragEvent): void {
    if (!uploadImage() || !windowDragHasFiles(e)) return;
    // Keep the browser from navigating away when the drop lands outside the composer.
    e.preventDefault();
  }

  function handleWindowDragLeave(e: DragEvent): void {
    if (!uploadImage() || !windowDragHasFiles(e)) return;
    windowDragDepth = Math.max(0, windowDragDepth - 1);
    if (windowDragDepth === 0) isDragOver.value = false;
  }

  function handleWindowDrop(e: DragEvent): void {
    windowDragDepth = 0;
    isDragOver.value = false;
    if (!uploadImage()) return;
    e.preventDefault();
    const files = Array.from(e.dataTransfer?.files ?? []);
    void addFiles(files);
  }

  /** Revoke every object URL and drop all attachments for the current session
      (called after submit/steer). */
  function clearSessionAttachments(sid: string): void {
    const removed = attachmentsBySession.value[sid] ?? [];
    for (const att of removed) {
      revokeAttachment(att);
    }
    const next = { ...attachmentsBySession.value };
    delete next[sid];
    attachmentsBySession.value = next;
    if (previewAttachment.value && removed.some(
      (att) => att.localId === previewAttachment.value?.localId
    )) previewAttachment.value = null;
  }

  /** Move server-accepted pending attachments to a newly authoritative scope.
      Uploads still in flight cannot be rebound: their old scope generation is
      rejected by Focus, so drop those transient chips instead of leaving a
      permanent spinner in the destination composer. */
  function rebindSessionAttachments(sourceSid: string, targetSid: string): void {
    if (!sourceSid || !targetSid || sourceSid === targetSid) return;
    const source = attachmentsBySession.value[sourceSid] ?? [];
    if (source.length === 0) return;
    const movable = source.filter((att) => !!att.fileId && !att.uploading && !att.error);
    const discarded = source.filter((att) => !movable.includes(att));
    for (const att of discarded) revokeAttachment(att);

    const destination = attachmentsBySession.value[targetSid] ?? [];
    const movedFileIds = new Set(movable.map((att) => att.fileId).filter(Boolean));
    const movedLocalIds = new Set(movable.map((att) => att.localId));
    const retainedDestination = destination.filter((att) => (
      !movedLocalIds.has(att.localId)
      && (!att.fileId || !movedFileIds.has(att.fileId))
    ));
    const next = { ...attachmentsBySession.value };
    delete next[sourceSid];
    if (movable.length > 0 || retainedDestination.length > 0) {
      next[targetSid] = [...retainedDestination, ...movable];
    }
    attachmentsBySession.value = next;
    if (
      previewAttachment.value
      && discarded.some((att) => att.localId === previewAttachment.value?.localId)
    ) previewAttachment.value = null;
  }

  /** Advance completed chips already owned by one semantic thread/draft to
      its newly authoritative CAS generation. This does not rebind backend
      metadata: the semantic scope is unchanged. Any old-generation upload
      still in flight is deliberately discarded because Focus rejects it. */
  function adoptSessionGeneration(targetSid: string): void {
    const target = parseAttachmentSessionId(targetSid);
    if (!target) return;
    const sources = Object.keys(attachmentsBySession.value)
      .map((sid) => ({ sid, parsed: parseAttachmentSessionId(sid) }))
      .filter((entry): entry is { sid: string; parsed: ParsedAttachmentSessionId } => (
        entry.sid !== targetSid
        && entry.parsed !== null
        && entry.parsed.semanticKey === target.semanticKey
        && entry.parsed.generation < target.generation
      ))
      .sort((left, right) => left.parsed.generation - right.parsed.generation);
    for (const source of sources) {
      rebindSessionAttachments(source.sid, targetSid);
    }
  }

  function clearAfterSubmit(): void {
    const sid = sessionId() ?? '';
    clearSessionAttachments(sid);
  }

  function patchAttachment(sid: string, localId: string, patch: Partial<Attachment>): void {
    const current = attachmentsBySession.value[sid] ?? [];
    if (!current.some((a) => a.localId === localId)) return;
    setForSession(
      sid,
      current.map((a) => (a.localId === localId ? { ...a, ...patch } : a)),
    );
  }

  function urlToBlob(url: string): Promise<Blob> {
    return fetch(url).then((r) => {
      if (!r.ok) throw new Error(`fetch failed: ${r.status}`);
      return r.blob();
    });
  }

  /** Refill the attachment strip from already-uploaded files (used when a queued
   *  prompt or an undone message is loaded back into the composer). The fileIds
   *  are reused directly (no re-upload); for a protected getFileUrl preview we
   *  fetch an authenticated blob URL so the thumbnail doesn't 401. Replaces any
   *  unsent draft attachments (mirroring loadForEdit(text), which overwrites) so
   *  a later submit sends exactly the edited message's files, not a mix. */
  function loadAttachments(atts: { fileId?: string; kind: 'image' | 'video' | 'file'; url: string; name?: string }[]): void {
    const sid = sessionId() ?? '';
    for (const existing of attachmentsBySession.value[sid] ?? []) revokeAttachment(existing);
    setForSession(sid, []);
    for (const att of atts) {
      const localId = nextLocalId();
      // Old drafts can contain `video` from the Kimi-derived type surface;
      // keep them resendable but never revive a browser player.
      const kind = att.kind === 'image' ? 'image' : 'file';
      const isData = /^data:/i.test(att.url);
      const isBlob = /^blob:/i.test(att.url);
      const name = att.name ?? kind;

      if (att.fileId) {
        // Ready as-is; fetch an authenticated thumbnail for protected URLs.
        // File attachments have no thumbnail — nothing to fetch or revoke.
        const entry: Attachment = {
          localId,
          name,
          kind,
          previewUrl: kind === 'image' ? att.url : undefined,
          uploading: false,
          fileId: att.fileId,
        };
        setForSession(sid, [...(attachmentsBySession.value[sid] ?? []), entry]);
        if (kind === 'image' && !isData && !isBlob) {
          const loadBlob = downloadFile?.();
          if (!loadBlob) continue;
          void loadBlob(att.fileId).then((blob) => {
            const blobUrl = URL.createObjectURL(blob);
            const current = attachmentsBySession.value[sid] ?? [];
            if (!current.some((a) => a.localId === localId)) {
              URL.revokeObjectURL(blobUrl);
              return;
            }
            patchAttachment(sid, localId, { previewUrl: blobUrl });
          }).catch(() => {
            // Keep the fallback previewUrl (honest broken state if it 401s).
          });
        }
      } else {
        // No fileId (e.g. a server-base64-inlined image, or a URL-backed source
        // from the wire/REST prompt path): re-upload the URL so the chip is
        // actually resendable — otherwise handleSubmit silently drops it. If the
        // URL can't be fetched (CORS / non-2xx) or upload is unavailable, skip
        // the chip rather than show a misleading ready attachment.
        // No URL at all (the non-clickable chip rebuilt from an inline-base64
        // notice): skip too — fetch('') would resolve to the current page and
        // upload the web app's HTML as the attachment.
        if (!att.url) continue;
        const upload = uploadImage();
        if (!upload) continue;
        const entry: Attachment = {
          localId,
          name,
          kind,
          previewUrl: kind === 'image' ? att.url : undefined,
          uploading: true,
        };
        setForSession(sid, [...(attachmentsBySession.value[sid] ?? []), entry]);
        void urlToBlob(att.url)
          .then((blob) => {
            const fname = name.includes('.') ? name : `${name}.${blob.type.split('/')[1] ?? 'bin'}`;
            return upload(blob, fname);
          })
          .then((result) => {
            if (result === null) {
              const current = attachmentsBySession.value[sid] ?? [];
              setForSession(sid, current.filter((a) => a.localId !== localId));
              return;
            }
            patchAttachment(sid, localId, { uploading: false, fileId: result.fileId });
          })
          .catch(() => {
            const current = attachmentsBySession.value[sid] ?? [];
            setForSession(sid, current.filter((a) => a.localId !== localId));
          });
      }
    }
  }

  // Close the preview lightbox when switching sessions — it may reference an
  // attachment that belongs to the previous session.
  watch(sessionId, () => {
    previewAttachment.value = null;
  });

  onMounted(() => {
    document.addEventListener('paste', handleDocumentPaste);
    document.addEventListener('dragenter', handleWindowDragEnter);
    document.addEventListener('dragover', handleWindowDragOver);
    document.addEventListener('dragleave', handleWindowDragLeave);
    document.addEventListener('drop', handleWindowDrop);
  });

  // Revoke all object URLs (every session) and remove the global listener on unmount.
  onUnmounted(() => {
    document.removeEventListener('paste', handleDocumentPaste);
    document.removeEventListener('dragenter', handleWindowDragEnter);
    document.removeEventListener('dragover', handleWindowDragOver);
    document.removeEventListener('dragleave', handleWindowDragLeave);
    document.removeEventListener('drop', handleWindowDrop);
    for (const atts of Object.values(attachmentsBySession.value)) {
      for (const att of atts) revokeAttachment(att);
    }
    previewAttachment.value = null;
  });

  return {
    attachments,
    previewAttachment,
    fileInputRef,
    isDragOver,
    removeAttachment,
    openAttachmentPreview,
    closeAttachmentPreview,
    openFilePicker,
    handleFileInputChange,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    clearAfterSubmit,
    clearSessionAttachments,
    rebindSessionAttachments,
    adoptSessionGeneration,
    loadAttachments,
  };
}

export type AttachmentUploadController = ReturnType<typeof useAttachmentUpload>;
