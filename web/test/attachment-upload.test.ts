import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ref } from 'vue';
import {
  useAttachmentUpload,
  type Attachment,
  type UploadedAttachment,
} from '../src/composables/useAttachmentUpload';

// The composable registers its paste listener and cleanup via onMounted /
// onUnmounted. Outside a component (unit test) there is no active instance, so
// Vue would warn; stub the two hooks since these tests don't exercise the
// lifecycle itself.
vi.mock('vue', async (importOriginal) => {
  const actual = await importOriginal<typeof import('vue')>();
  return { ...actual, onMounted: vi.fn(), onUnmounted: vi.fn() };
});

type UploadImage = (
  file: Blob,
  name?: string,
) => Promise<UploadedAttachment | null>;

function setup(
  uploadImage?: UploadImage,
  sessionId: string | null = 'test-session',
  downloadFile?: (fileId: string) => Promise<Blob>,
) {
  return useAttachmentUpload({
    uploadImage: () => uploadImage,
    downloadFile: () => downloadFile,
    sessionId: () => sessionId ?? undefined,
  });
}

function imageFile(name: string): File {
  return { name, type: 'image/png' } as unknown as File;
}

function videoFile(name: string): File {
  return { name, type: 'video/mp4' } as unknown as File;
}

function inputEvent(files: File[]): Event {
  return { target: { files, value: 'x' } } as unknown as Event;
}

describe('useAttachmentUpload', () => {
  let createObjectURL: ReturnType<typeof vi.fn>;
  let revokeObjectURL: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    createObjectURL = vi.fn().mockReturnValue('blob:mock-url');
    revokeObjectURL = vi.fn();
    (globalThis.URL as unknown as { createObjectURL: unknown }).createObjectURL = createObjectURL;
    (globalThis.URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = revokeObjectURL;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('waits for a verified server preview URL before rendering a picked image', async () => {
    let resolveUpload: ((value: UploadedAttachment | null) => void) | undefined;
    const uploadImage = vi.fn<UploadImage>().mockImplementation(
      () => new Promise<UploadedAttachment | null>((resolve) => { resolveUpload = resolve; }),
    );
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([imageFile('a.png')]));

    expect(att.attachments.value).toHaveLength(1);
    expect(att.attachments.value[0]).toMatchObject({ name: 'a.png', kind: 'image', uploading: true });
    expect(att.attachments.value[0].previewUrl).toBeUndefined();
    expect(createObjectURL).not.toHaveBeenCalled();

    expect(resolveUpload).toBeTypeOf('function');
    resolveUpload?.({
      fileId: 'f1',
      name: 'a.png',
      mediaType: 'image/png',
      previewUrl: '/api/attachments/f1',
    });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(att.attachments.value[0]).toMatchObject({
      fileId: 'f1',
      kind: 'image',
      uploading: false,
      previewUrl: '/api/attachments/f1',
    });
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('fails closed when a Focus image response lacks its controlled preview URL', async () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue({
      fileId: 'f1',
      name: 'a.png',
      mediaType: 'image/png',
      previewUrl: '',
      localImagePreviewAllowed: false,
    });
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([imageFile('a.png')]));

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(att.attachments.value[0]).toMatchObject({
      fileId: 'f1',
      kind: 'file',
      uploading: false,
    });
    expect(att.attachments.value[0].previewUrl).toBeUndefined();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('accepts a non-media file as a file attachment without a thumbnail object URL', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue({ fileId: 'f1', name: 'a.pdf', mediaType: 'application/pdf' });
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([{ name: 'a.pdf', type: 'application/pdf' } as unknown as File]));

    expect(att.attachments.value).toHaveLength(1);
    expect(att.attachments.value[0]).toMatchObject({
      name: 'a.pdf',
      kind: 'file',
      mediaType: 'application/pdf',
      uploading: true,
    });
    // No thumbnail for generic files — the chip renders an icon instead.
    expect(att.attachments.value[0].previewUrl).toBeUndefined();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('treats video as an ordinary attachment without a browser preview', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue({ fileId: 'f1', name: 'clip.mp4', mediaType: 'video/mp4' });
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([videoFile('clip.mp4')]));

    expect(att.attachments.value[0]).toMatchObject({
      name: 'clip.mp4',
      kind: 'file',
      mediaType: 'video/mp4',
    });
    expect(att.attachments.value[0].previewUrl).toBeUndefined();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('accepts a file with an empty MIME type as a file attachment', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([{ name: 'Makefile', type: '' } as unknown as File]));
    expect(att.attachments.value).toHaveLength(1);
    expect(att.attachments.value[0].kind).toBe('file');
    // The wire schema requires a non-empty media_type — '' must be normalized.
    expect(att.attachments.value[0].mediaType).toBe('application/octet-stream');
  });

  it('is a no-op when uploadImage is not provided', () => {
    const att = setup(undefined);
    att.handleFileInputChange(inputEvent([imageFile('a.png')]));
    expect(att.attachments.value).toHaveLength(0);
  });

  it('removeAttachment on a file chip has no object URL to revoke', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([{ name: 'a.pdf', type: 'application/pdf' } as unknown as File]));
    const localId = att.attachments.value[0].localId;

    att.removeAttachment(localId);
    expect(att.attachments.value).toHaveLength(0);
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });

  it('loadAttachments refills a file attachment without fetching a thumbnail', () => {
    const att = setup(undefined);
    att.loadAttachments([
      { fileId: 'f_pdf', kind: 'file', url: 'https://example.test/api/v1/files/f_pdf', name: 'a.pdf' },
    ]);
    expect(att.attachments.value).toHaveLength(1);
    expect(att.attachments.value[0]).toMatchObject({
      fileId: 'f_pdf',
      kind: 'file',
      name: 'a.pdf',
      uploading: false,
    });
    expect(att.attachments.value[0].previewUrl).toBeUndefined();
  });

  it('does not discover an implicit transport when an image loader is absent', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const att = setup(undefined);

    att.loadAttachments([
      { fileId: 'f_image', kind: 'image', url: '/api/attachments/f_image', name: 'a.png' },
    ]);
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(att.attachments.value[0]).toMatchObject({
      fileId: 'f_image',
      previewUrl: '/api/attachments/f_image',
      uploading: false,
    });
  });

  it('uses the explicitly supplied image loader for a protected preview', async () => {
    const blob = new Blob(['image'], { type: 'image/png' });
    const downloadFile = vi.fn().mockResolvedValue(blob);
    const att = setup(undefined, 'test-session', downloadFile);

    att.loadAttachments([
      { fileId: 'f_image', kind: 'image', url: '/api/attachments/f_image', name: 'a.png' },
    ]);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(downloadFile).toHaveBeenCalledWith('f_image');
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(att.attachments.value[0].previewUrl).toBe('blob:mock-url');
  });

  it('normalizes a legacy video draft to an inert file attachment', () => {
    const att = setup(undefined);
    att.loadAttachments([
      { fileId: 'f_video', kind: 'video', url: '/api/attachments/f_video', name: 'clip.mp4' },
    ]);

    expect(att.attachments.value[0]).toMatchObject({
      fileId: 'f_video',
      kind: 'file',
      name: 'clip.mp4',
    });
    expect(att.attachments.value[0].previewUrl).toBeUndefined();
  });

  it('removeAttachment drops a pending image without ever creating a local preview URL', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([imageFile('a.png')]));
    const localId = att.attachments.value[0].localId;

    att.removeAttachment(localId);
    expect(att.attachments.value).toHaveLength(0);
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });

  it('removeAttachment also closes the preview when it shows the removed entry', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([imageFile('a.png')]));
    const added = att.attachments.value[0];
    att.openAttachmentPreview(added);
    expect(att.previewAttachment.value).not.toBeNull();

    att.removeAttachment(added.localId);
    expect(att.previewAttachment.value).toBeNull();
  });

  it('openAttachmentPreview / closeAttachmentPreview toggle the preview', () => {
    const att = setup(undefined);
    const item: Attachment = { localId: 'x', name: 'a.png', kind: 'image', previewUrl: 'blob:x', uploading: false };
    att.openAttachmentPreview(item);
    expect(att.previewAttachment.value?.localId).toBe('x');
    att.closeAttachmentPreview();
    expect(att.previewAttachment.value).toBeNull();
  });

  it('clearAfterSubmit revokes retained object URLs and empties the list', () => {
    const att = setup(undefined);
    att.loadAttachments([
      { fileId: 'a', kind: 'image', url: 'blob:first', name: 'a.png' },
      { fileId: 'b', kind: 'image', url: 'blob:second', name: 'b.png' },
    ]);
    expect(att.attachments.value).toHaveLength(2);

    att.clearAfterSubmit();
    expect(att.attachments.value).toHaveLength(0);
    expect(revokeObjectURL).toHaveBeenCalledTimes(2);
  });

  it('loadAttachments refills an already-uploaded attachment without re-uploading', () => {
    const att = setup(undefined);
    att.loadAttachments([
      { fileId: 'f_existing', kind: 'image', url: 'data:image/png;base64,AAAA', name: 'a.png' },
    ]);
    expect(att.attachments.value).toHaveLength(1);
    expect(att.attachments.value[0]).toMatchObject({
      fileId: 'f_existing',
      kind: 'image',
      name: 'a.png',
      uploading: false,
      previewUrl: 'data:image/png;base64,AAAA',
    });
  });

  it('loadAttachments replaces any unsent draft attachments instead of appending', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([imageFile('draft.png')]));
    expect(att.attachments.value).toHaveLength(1);

    att.loadAttachments([
      { fileId: 'f_existing', kind: 'image', url: 'data:image/png;base64,AAAA', name: 'refill.png' },
    ]);
    expect(att.attachments.value).toHaveLength(1);
    expect(att.attachments.value[0].name).toBe('refill.png');
  });

  it('loadAttachments with an empty list clears the attachment strip', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const att = setup(uploadImage);
    att.handleFileInputChange(inputEvent([imageFile('draft.png')]));
    expect(att.attachments.value).toHaveLength(1);

    att.loadAttachments([]);
    expect(att.attachments.value).toHaveLength(0);
  });

  it('loadAttachments re-uploads a fileId-less data URL so it becomes resendable', async () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue({ fileId: 'f_new', name: 'a.png', mediaType: 'image/png' });
    const att = setup(uploadImage);
    const blob = new Blob(['x'], { type: 'image/png' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) }));

    att.loadAttachments([{ kind: 'image', url: 'data:image/png;base64,AAAA', name: 'a.png' }]);
    expect(att.attachments.value).toHaveLength(1);
    expect(att.attachments.value[0].uploading).toBe(true);

    // Flush the fetch → blob → upload promise chain so the re-upload resolves.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(att.attachments.value[0].uploading).toBe(false);
    expect(att.attachments.value[0].fileId).toBe('f_new');
    expect(uploadImage).toHaveBeenCalledOnce();
  });

  it('loadAttachments skips a fileId-less data URL when re-upload is unavailable', () => {
    const att = setup(undefined);
    att.loadAttachments([{ kind: 'image', url: 'data:image/png;base64,AAAA', name: 'a.png' }]);
    expect(att.attachments.value).toHaveLength(0);
  });

  it('loadAttachments re-uploads a fileId-less http URL so it becomes resendable', async () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue({ fileId: 'f_http', name: 'x.png', mediaType: 'image/png' });
    const att = setup(uploadImage);
    const blob = new Blob(['x'], { type: 'image/png' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) }));

    att.loadAttachments([{ kind: 'image', url: 'https://example.test/x.png', name: 'x.png' }]);
    expect(att.attachments.value).toHaveLength(1);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(att.attachments.value[0].fileId).toBe('f_http');
  });

  it('loadAttachments drops a fileId-less URL whose fetch fails', async () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue({ fileId: 'f_x', name: 'x.png', mediaType: 'image/png' });
    const att = setup(uploadImage);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401 }));

    att.loadAttachments([{ kind: 'image', url: 'https://example.test/protected.png', name: 'protected.png' }]);
    expect(att.attachments.value).toHaveLength(1);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(att.attachments.value).toHaveLength(0);
  });

  it('isolates attachments between sessions', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const sessionId = ref<string | undefined>('sess-a');
    const att = useAttachmentUpload({ uploadImage: () => uploadImage, sessionId: () => sessionId.value });

    att.handleFileInputChange(inputEvent([imageFile('a.png')]));
    expect(att.attachments.value).toHaveLength(1);

    // Switch to session B — A's attachment must not show up here.
    sessionId.value = 'sess-b';
    expect(att.attachments.value).toHaveLength(0);
    att.handleFileInputChange(inputEvent([imageFile('b.png')]));
    expect(att.attachments.value).toHaveLength(1);

    // Switch back to A — its attachment is still there.
    sessionId.value = 'sess-a';
    expect(att.attachments.value).toHaveLength(1);
    expect(att.attachments.value[0].name).toBe('a.png');

    // B's attachment is gone from A's view.
    expect(att.attachments.value.map((a) => a.name)).not.toContain('b.png');
  });

  it('forgets an explicitly invalidated old composer scope', () => {
    const sessionId = ref<string | undefined>('scope-a');
    const att = useAttachmentUpload({
      uploadImage: () => undefined,
      sessionId: () => sessionId.value,
    });
    att.loadAttachments([
      { fileId: 'old-file', kind: 'file', url: '', name: 'old.txt' },
    ]);
    sessionId.value = 'scope-b';

    att.clearSessionAttachments('scope-a');
    sessionId.value = 'scope-a';

    expect(att.attachments.value).toEqual([]);
  });

  it('rebinds accepted chips to the server-confirmed composer scope', () => {
    const sessionId = ref<string | undefined>('thread:one');
    const att = useAttachmentUpload({
      uploadImage: () => undefined,
      sessionId: () => sessionId.value,
    });
    att.loadAttachments([
      { fileId: 'thread-file', kind: 'file', url: '', name: 'thread.txt' },
    ]);
    sessionId.value = 'draft:/work';
    att.loadAttachments([
      { fileId: 'draft-file', kind: 'file', url: '', name: 'draft.txt' },
    ]);

    att.rebindSessionAttachments('thread:one', 'draft:/work');

    expect(att.attachments.value.map((item) => item.fileId)).toEqual([
      'draft-file',
      'thread-file',
    ]);
    sessionId.value = 'thread:one';
    expect(att.attachments.value).toEqual([]);
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });

  it('drops an in-flight chip when its old generation cannot be rebound', async () => {
    let resolveUpload: ((value: UploadedAttachment | null) => void) | undefined;
    const uploadImage = vi.fn<UploadImage>().mockImplementation(
      () => new Promise<UploadedAttachment | null>((resolve) => { resolveUpload = resolve; }),
    );
    const sessionId = ref<string | undefined>('thread:one');
    const att = useAttachmentUpload({
      uploadImage: () => uploadImage,
      sessionId: () => sessionId.value,
    });
    att.handleFileInputChange(inputEvent([imageFile('late.png')]));

    att.rebindSessionAttachments('thread:one', 'draft:/work');
    sessionId.value = 'draft:/work';
    expect(att.attachments.value).toEqual([]);

    resolveUpload?.({
      fileId: 'late-file',
      name: 'late.png',
      mediaType: 'image/png',
      previewUrl: '/api/attachments/late-file',
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(att.attachments.value).toEqual([]);
  });

  it('keeps accepted chips across Composer instances and advances only their semantic scope', () => {
    const sessionId = ref<string | undefined>(
      'tab-1:generation:1:thread:thread-1',
    );
    // ConversationPane creates this controller once. The empty and docked
    // Composer instances consume the same object on opposite sides of their
    // render transition.
    const paneOwned = useAttachmentUpload({
      uploadImage: () => undefined,
      sessionId: () => sessionId.value,
    });
    const emptyComposerConsumer = paneOwned;
    emptyComposerConsumer.loadAttachments([
      { fileId: 'accepted-a', kind: 'file', url: '', name: 'a.txt' },
    ]);

    sessionId.value = 'tab-1:generation:2:thread:thread-2';
    paneOwned.loadAttachments([
      { fileId: 'accepted-b', kind: 'file', url: '', name: 'b.txt' },
    ]);
    sessionId.value = 'tab-1:generation:3:thread:thread-1';
    paneOwned.adoptSessionGeneration(sessionId.value);
    const dockedComposerConsumer = paneOwned;

    expect(dockedComposerConsumer.attachments.value.map((item) => item.fileId)).toEqual([
      'accepted-a',
    ]);
    sessionId.value = 'tab-1:generation:2:thread:thread-2';
    expect(paneOwned.attachments.value.map((item) => item.fileId)).toEqual([
      'accepted-b',
    ]);
  });

  it('drops an old-generation in-flight chip while revisiting the same semantic thread', async () => {
    let resolveUpload: ((value: UploadedAttachment | null) => void) | undefined;
    const uploadImage = vi.fn<UploadImage>().mockImplementation(
      () => new Promise<UploadedAttachment | null>((resolve) => { resolveUpload = resolve; }),
    );
    const sessionId = ref<string | undefined>('tab-1:generation:1:thread:thread-1');
    const paneOwned = useAttachmentUpload({
      uploadImage: () => uploadImage,
      sessionId: () => sessionId.value,
    });
    paneOwned.handleFileInputChange(inputEvent([imageFile('late.png')]));

    sessionId.value = 'tab-1:generation:3:thread:thread-1';
    paneOwned.adoptSessionGeneration(sessionId.value);
    expect(paneOwned.attachments.value).toEqual([]);

    resolveUpload?.({
      fileId: 'too-late',
      name: 'late.png',
      mediaType: 'image/png',
      previewUrl: '/api/attachments/too-late',
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(paneOwned.attachments.value).toEqual([]);
  });

  it('adds dropped files once and stops the drop from bubbling to document handlers', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const att = setup(uploadImage);
    const file = { name: 'd.txt', type: 'text/plain' } as unknown as File;
    const preventDefault = vi.fn();
    const stopPropagation = vi.fn();

    att.handleDrop({
      dataTransfer: { files: [file] },
      preventDefault,
      stopPropagation,
    } as unknown as DragEvent);

    // The document-level drop listener must not see the same drop again —
    // otherwise the file would be attached twice.
    expect(preventDefault).toHaveBeenCalled();
    expect(stopPropagation).toHaveBeenCalled();
    expect(att.attachments.value).toHaveLength(1);
    expect(att.attachments.value[0]).toMatchObject({ name: 'd.txt', kind: 'file' });
  });

  it('ignores a dragover that carries no files (e.g. text drag)', () => {
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const att = setup(uploadImage);
    const preventDefault = vi.fn();

    att.handleDragOver({
      dataTransfer: { items: [{ kind: 'string' }] },
      preventDefault,
      stopPropagation: vi.fn(),
    } as unknown as DragEvent);

    expect(preventDefault).not.toHaveBeenCalled();
    expect(att.isDragOver.value).toBe(false);
  });

  it('skips a file attachment with no fileId and an empty URL instead of fetching it', async () => {
    // The non-clickable chip rebuilt from an inline-base64 notice has neither —
    // fetch('') would resolve to the current page and upload the web app HTML.
    const uploadImage = vi.fn<UploadImage>().mockResolvedValue(null);
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const att = setup(uploadImage);

    att.loadAttachments([{ kind: 'file', url: '', name: 'image.avif' }]);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(att.attachments.value).toHaveLength(0);
    // No fetch with the empty URL (a same-document fetch would upload the page).
    expect(fetchSpy.mock.calls.every((call) => call[0] !== '')).toBe(true);
    fetchSpy.mockRestore();
  });
});
