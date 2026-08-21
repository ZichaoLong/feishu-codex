import { watch, type Ref } from 'vue';
import type { PromptAttachment } from '../../types';

interface ComposerSemanticAttachment {
  localId: string;
  fileId?: string;
  kind: string;
  name: string;
  mediaType?: string;
  size?: number;
  uploading: boolean;
  error?: boolean;
}

function attachmentFingerprint(
  attachments: readonly ComposerSemanticAttachment[],
): string {
  return JSON.stringify(attachments.map((attachment) => [
    attachment.localId,
    attachment.fileId,
    attachment.kind,
    attachment.name,
    attachment.mediaType,
    attachment.size,
    attachment.uploading,
    attachment.error,
  ]));
}

/** Monotonic ownership token: returning to equal values never revives an old submit. */
export function useComposerSubmissionRevision(options: {
  text: Readonly<Ref<string>>;
  attachments: Readonly<Ref<readonly ComposerSemanticAttachment[]>>;
  sessionId: () => string | undefined;
  composerReady: () => boolean;
}) {
  let revision = 0;
  const advance = () => { revision += 1; };
  watch(options.text, advance, { flush: 'sync' });
  watch(options.sessionId, advance, { flush: 'sync' });
  watch(options.composerReady, advance, { flush: 'sync' });
  watch(
    () => attachmentFingerprint(options.attachments.value),
    advance,
    { flush: 'sync' },
  );
  return {
    capture: () => revision,
    isCurrent: (captured: number) => revision === captured,
  };
}

export interface ComposerSubmission {
  text: string;
  attachments: PromptAttachment[];
  matchesCurrent(): boolean;
  commit(): boolean;
  retainTextOnly(): boolean;
  retain(): void;
}

/**
 * Keeps one Composer payload pending until its caller knows whether a local
 * request boundary accepted it. The owner never clears a newer local draft.
 */
export function createComposerSubmission(
  payload: Pick<ComposerSubmission, 'text' | 'attachments'>,
  matchesCurrent: () => boolean,
  commitCurrent: () => void,
  clearCurrentAttachments: () => void,
  settled: () => void,
): ComposerSubmission {
  let state: 'pending' | 'committed' | 'retained' = 'pending';

  function retain(): void {
    if (state !== 'pending') return;
    state = 'retained';
    settled();
  }

  function commit(): boolean {
    if (state === 'committed') return true;
    if (state !== 'pending') return false;
    if (!matchesCurrent()) {
      retain();
      return false;
    }
    state = 'committed';
    commitCurrent();
    settled();
    return true;
  }

  function retainTextOnly(): boolean {
    if (state !== 'pending') return false;
    if (!matchesCurrent()) {
      retain();
      return false;
    }
    try {
      clearCurrentAttachments();
    } catch {
      // Attachment cleanup may have partially mutated the exact draft. Make
      // that payload terminal before the conservative full clear; never
      // re-check after a partial cleanup has advanced the semantic revision.
      state = 'committed';
      commitCurrent();
      settled();
      return false;
    }
    state = 'retained';
    settled();
    return true;
  }

  return {
    text: payload.text,
    attachments: payload.attachments,
    matchesCurrent: () => state === 'pending' && matchesCurrent(),
    commit,
    retainTextOnly,
    retain,
  };
}
